"""`betdata nba ...`: NBA history (fetch on Actions, export, load) and, later, the model."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import typer

nba_app = typer.Typer(help="NBA data and model (M14).", no_args_is_help=True)
log = logging.getLogger(__name__)


def _engine():
    from betdata.config import get_settings
    from betdata.db import seed as seed_mod
    from betdata.db.engine import make_engine, upgrade

    settings = get_settings()
    upgrade(settings.db_url)
    engine = make_engine(settings.db_url)
    seed_mod.seed(engine)
    return settings, engine


def _run(settings, engine, source: str, job: str, **params):
    from betdata.sources import REGISTRY

    src = REGISTRY[source](engine, settings.source(source))
    try:
        res = src.run(job, **params)
    finally:
        src.close()
    typer.echo(f"{source} {job} {params}: {res.status} {res.rows_inserted}/{res.rows_seen} "
               f"{res.message}")  # fmt: skip
    return res


@nba_app.command("history-fetch")
def history_fetch(season: int = typer.Option(..., help="Season (start year), e.g. 2023."),
                  odds: bool = True, injuries: bool = True):  # fmt: skip
    """Fetch one season's history that needs Actions: the schedule (sportsdataverse), then
    ESPN's opening/closing lines and the official injury reports, month by month."""
    settings, engine = _engine()
    _run(settings, engine, "sportsdataverse", "schedule", sport="nba", seasons=str(season))
    if odds:
        _run(settings, engine, "espn_nba", "odds_history", season=season, limit=5000)
    if injuries and season >= 2018:
        day, end = date(season, 10, 1), min(date(season + 1, 10, 15), datetime.now(UTC).date())
        if season == 2019:
            end = date(2020, 10, 15)  # the bubble ran to October 2020
        while day <= end:
            last = min(end, (day.replace(day=1) + timedelta(days=32)).replace(day=1)
                       - timedelta(days=1))  # fmt: skip
            try:
                _run(settings, engine, "nba_injury", "history", start=day.isoformat(),
                     end=last.isoformat())  # fmt: skip
            except Exception as e:  # keep the other months; the run is logged as failed
                typer.echo(f"nba_injury history {day}..{last}: crashed: {e!r}")
            day = last + timedelta(days=1)


@nba_app.command("export-history")
def export_history(season: int = typer.Option(...), out: Path = typer.Option(Path("history"))):
    """Write this database's NBA lines and injury reports for one season as gzipped CSV."""
    from betdata.nba.history import export

    _, engine = _engine()
    typer.echo(json.dumps(export(engine, out, season)))


@nba_app.command("load-history")
def load_history(src: Path = typer.Argument(..., help="Directory of exported files.")):
    """Load exported lines and injury reports (no network). Needs the NBA schedule loaded."""
    from betdata.nba.history import load

    _, engine = _engine()
    typer.echo(json.dumps(load(engine, src)))


# --- the model (M14) -----------------------------------------------------------------------

REPORTS = Path("data/reports")


def _rows(rebuild: bool = False):
    """The store, its outcomes, training play probabilities and every feature row (all groups,
    both points, 2015-2025), cached under data/derived."""
    import pickle

    from betdata.nba.data import load_store, outcomes
    from betdata.nba.features import GROUPS
    from betdata.nba.pipeline import TRAIN, build, fit_play_probs

    _, engine = _engine()
    with engine.connect() as conn:
        store = load_store(conn)
    out = outcomes(store)
    cache = Path("data/derived/nba_rows.pkl")
    if cache.exists() and not rebuild:
        rows, play = pickle.loads(cache.read_bytes())
    else:
        play = fit_play_probs(store, TRAIN)
        store = store.with_params(play_prob=play)
        gids = [g for g in store.games.index if g in out.index and store.games.at[g, "season"]
                <= 2025]  # fmt: skip
        rows = build(store, list(GROUPS.values()), gids)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(pickle.dumps((rows, play)))
    return store.with_params(play_prob=play), out, rows, play


@nba_app.command("select")
def select_cmd(rebuild: bool = False):
    """Add the feature groups one at a time on validation (2021-22, 2022-23); keep a group if it
    lowers margin MAE (total MAE for pace). Writes data/reports/m14_selection.json."""
    from betdata.nba.features import ORDER
    from betdata.nba.pipeline import columns, evaluate
    from betdata.nba.selection import VALIDATE, elo_mae

    _, out, rows, play = _rows(rebuild)
    log_rows = []
    m, t = columns(rows, [])
    base = evaluate(rows, out, m, t)
    log_rows.append({"step": "base", "margin": base.margin, "total": base.total,
                     "kept": True, "lam": [base.lam_m, base.lam_t]})  # fmt: skip
    kept, best = [], base
    for grp in ORDER:
        m, t = columns(rows, [*kept, grp])
        ev = evaluate(rows, out, m, t)
        better = ev.total < best.total if grp == "pace" else ev.margin < best.margin
        log_rows.append({"step": f"+{grp}", "margin": ev.margin, "total": ev.total,
                         "kept": bool(better), "lam": [ev.lam_m, ev.lam_t],
                         "by_point": ev.valid})  # fmt: skip
        if better:
            kept.append(grp)
            best = ev
    ablation = []
    for grp in kept:
        m, t = columns(rows, [g for g in kept if g != grp])
        ev = evaluate(rows, out, m, t)
        ablation.append({"without": grp, "margin": ev.margin, "total": ev.total})
    report = {"kept": kept, "final": {"margin": best.margin, "total": best.total,
                                      "lam": [best.lam_m, best.lam_t]},
              "elo_valid": elo_mae(rows, out, VALIDATE), "steps": log_rows,
              "ablation": ablation,
              "play_prob": {k: v for k, v in play.items()}}  # fmt: skip
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "m14_selection.json").write_text(json.dumps(report, indent=1, default=float))
    for r in log_rows:
        typer.echo(f"{r['step']:>8}  margin {r['margin']:.3f}  total {r['total']:.3f}  "
                   f"{'kept' if r['kept'] else 'dropped'}")  # fmt: skip
    for r in ablation:
        typer.echo(f" without {r['without']:>5}: margin {r['margin']:.3f} total {r['total']:.3f}")
    typer.echo(f"Elo baseline (validation): {report['elo_valid']}")


@nba_app.command("tune")
def tune_cmd():
    """On validation only: calibration check, betting thresholds, model+market blend weight.
    Writes config/nba_model.json (the frozen design)."""
    import numpy as np

    from betdata.moneyline import fit_platt
    from betdata.nba.pipeline import columns
    from betdata.nba.selection import (
        DESIGN_PATH,
        ML_THRESHOLDS,
        SPREAD_THRESHOLDS,
        TRAIN,
        VALIDATE,
        blend_weight,
        choose_threshold,
        fits_for,
        market_frame,
        ml_bets,
        predictions,
        spread_bets,
        win_probs,
    )

    store, out, rows, _ = _rows()
    sel = json.loads((REPORTS / "m14_selection.json").read_text())
    kept, (lam_m, lam_t) = sel["kept"], sel["final"]["lam"]
    m, t = columns(rows, kept)
    fits = fits_for(rows, out, m, t, TRAIN, lam_m, lam_t)
    pred = predictions(rows, out, fits, VALIDATE)
    mkt = market_frame(store, pred.game_id.unique())
    probs = win_probs(pred)
    y = (pred.margin > 0).to_numpy(dtype=float)
    slope, intercept = fit_platt(probs, y)
    over = slope < 1.0  # overconfident: shrink toward 50%
    calib = (slope, intercept) if over else None
    probs_c = win_probs(pred, calib)
    nv = mkt.loc[pred.game_id]
    # Validation has closing lines only (MODEL.md): bets there are graded at the close.
    sp_t, sp_by = choose_threshold(lambda th: spread_bets(pred, mkt, th, "close"),
                                   SPREAD_THRESHOLDS)  # fmt: skip
    ml_t, ml_by = choose_threshold(lambda th: ml_bets(pred, mkt, th, probs_c, "close"),
                                   ML_THRESHOLDS)  # fmt: skip
    # The blend weight needs a line known at the prediction point: 2015-16 and 2016-17 have
    # one (the "Opening" book). Chosen there with a model fitted on 2017-18 to 2020-21 only.
    early = (2015, 2016)
    fits_b = fits_for(rows, out, m, t, tuple(s for s in TRAIN if s not in early), lam_m, lam_t)
    pred_b = predictions(rows, out, fits_b, early)
    w, w_by = blend_weight(pred_b, market_frame(store, pred_b.game_id.unique()))
    bins = np.clip((probs_c * 10).astype(int), 0, 9)
    table = [{"bin": f"{b / 10:.1f}-{(b + 1) / 10:.1f}", "n": int((bins == b).sum()),
              "pred": float(probs_c[bins == b].mean()) if (bins == b).any() else None,
              "won": float(y[bins == b].mean()) if (bins == b).any() else None}
             for b in range(10)]  # fmt: skip
    design = {"groups": kept, "lam_m": lam_m, "lam_t": lam_t, "spread_threshold": sp_t,
              "ml_threshold": ml_t, "blend_weight": w, "fit": "",
              "ml_final_test_roi": None,
              "calibration": {"slope": slope, "intercept": intercept, "applied": over,
                              "fitted_on": "validation 2021-2022, both points"}}  # fmt: skip
    DESIGN_PATH.write_text(json.dumps(design, indent=2) + "\n")
    brier = float(np.mean((probs_c - y) ** 2))
    report = {"design": design, "brier_valid": brier, "calibration_table": table,
              "spread_thresholds": sp_by, "ml_thresholds": ml_by, "blend": w_by,
              "games_with_open_line": int(nv.open_spread_home.notna().sum()),
              "games_with_close_line": int(nv.close_spread_home.notna().sum())}  # fmt: skip
    (REPORTS / "m14_tune.json").write_text(json.dumps(report, indent=1, default=float))
    typer.echo(json.dumps({k: report[k] for k in ("brier_valid", "games_with_open_line",
                                                  "games_with_close_line")}))  # fmt: skip
    typer.echo(f"platt slope {slope:.3f} (applied: {over}); spread threshold {sp_t}; "
               f"ml threshold {ml_t}; blend weight {w}")  # fmt: skip


@nba_app.command("final-test")
def final_test_cmd(again: bool = typer.Option(False, help="Score again (only to reproduce).")):
    """Score the frozen design once on 2023-24 to 2025-26 after refitting on 2015-2022."""
    import numpy as np

    from betdata.nba.pipeline import columns
    from betdata.nba.selection import (
        DESIGN_PATH,
        TEST,
        TRAIN,
        VALIDATE,
        elo_mae,
        fits_for,
        market_frame,
        ml_bets,
        predictions,
        record,
        spread_bets,
        win_probs,
    )

    path = REPORTS / "m14_final_test.json"
    if path.exists() and not again:
        typer.echo(f"{path} exists: the final test is scored once (--again to reproduce)")
        raise typer.Exit(1)
    design = json.loads(DESIGN_PATH.read_text())
    store, out, rows, _ = _rows()
    m, t = columns(rows, design["groups"])
    fits = fits_for(rows, out, m, t, TRAIN + VALIDATE, design["lam_m"], design["lam_t"])
    pred = predictions(rows, out, fits, TEST)
    mkt = market_frame(store, pred.game_id.unique())
    c = design["calibration"]
    probs = win_probs(pred, (c["slope"], c["intercept"]) if c["applied"] else None)
    d = pred.join(mkt, on="game_id")
    y = (d.margin > 0).to_numpy(dtype=float)
    res = {"games": int(pred.game_id.nunique())}
    for p in ("morning", "pretip", "all"):
        s = d if p == "all" else d[d.point == p]
        has_close = s.close_spread_home.notna()
        res[p] = {
            "margin_mae": float(np.abs(s.pred - s.margin).mean()),
            "total_mae": float(np.abs(s.pred_total - s.total).mean()),
            "close_margin_mae": float(np.abs(-s.close_spread_home[has_close]
                                             - s.margin[has_close]).mean()),
            "model_mae_where_close": float(np.abs(s.pred[has_close]
                                                  - s.margin[has_close]).mean()),
            "close_total_mae": float(np.abs(s.close_total - s.total).dropna().mean()),
            "open_margin_mae": float(np.abs(-s.open_spread_home - s.margin).dropna().mean()),
            "blend_mae": float(np.abs(design["blend_weight"] * s.pred[s.open_spread_home.notna()]
                                      + (1 - design["blend_weight"])
                                      * -s.open_spread_home.dropna()
                                      - s.margin[s.open_spread_home.notna()]).mean()),
            "games_with_close": int(has_close.sum()),
        }  # fmt: skip
    res["elo_mae"] = elo_mae(rows, out, TEST)
    for at in ("open", "close"):  # at the prediction-time (opening) line, and at the close
        res[f"spread_bets_{at}"] = record(spread_bets(pred, mkt, design["spread_threshold"], at))
        res[f"moneyline_bets_{at}"] = record(ml_bets(pred, mkt, design["ml_threshold"], probs,
                                                     at))  # fmt: skip
    nvc = np.array([no_vig_or_nan(h, a) for h, a in zip(d.close_ml_home, d.close_ml_away,
                                                        strict=True)])  # fmt: skip
    ok = ~np.isnan(nvc)
    res["brier"] = {"model": float(np.mean((probs[ok] - y[ok]) ** 2)),
                    "market_close_novig": float(np.mean((nvc[ok] - y[ok]) ** 2)),
                    "games": int(ok.sum() // 2)}  # fmt: skip
    res["design"] = design
    REPORTS.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(res, indent=1, default=float))
    typer.echo(json.dumps(res, indent=1, default=float))


def no_vig_or_nan(h, a) -> float:
    import math

    from betdata.moneyline import no_vig

    if h is None or a is None or h != h or a != a:
        return math.nan
    nv = no_vig(int(h), int(a))
    return nv[0] if nv else math.nan


@nba_app.command("freeze")
def freeze_cmd():
    """Fit the frozen design on every completed season (2015-2025) and commit it as the live
    fit under config/fits/ (the live runner never refits)."""
    from betdata.nba.pipeline import columns
    from betdata.nba.selection import DESIGN_PATH, fits_for, save_fit

    design = json.loads(DESIGN_PATH.read_text())
    _, out, rows, play = _rows()
    seasons = tuple(range(2015, 2026))
    m, t = columns(rows, design["groups"])
    fits = fits_for(rows, out, m, t, seasons, design["lam_m"], design["lam_t"])
    path = save_fit(fits, play, seasons, design)
    design["fit"] = str(path.relative_to(Path(__file__).resolve().parents[3]))
    DESIGN_PATH.write_text(json.dumps(design, indent=2) + "\n")
    typer.echo(f"wrote {path}")


@nba_app.command("replay")
def replay_cmd(season: int = typer.Option(2026), out: Path = typer.Option(None)):
    """The 2026-27 games played so far with the frozen live model: the prediction at each
    point, as of that point, against the market line known then and the result."""
    from betdata.nba.data import load_store, outcomes
    from betdata.nba.live import Design, load_fit, predict_game
    from betdata.nba.points import POINTS, point_time

    design = Design.load()
    fits, play, version = load_fit(design)
    _, engine = _engine()
    with engine.connect() as conn:
        store = load_store(conn, min_season=season - 3).with_params(play_prob=play)
    res = outcomes(store)
    g = store.games
    played = [i for i in g.index if g.at[i, "season"] == season and i in res.index]
    odds = store.frame("odds").df
    rows = []
    for gid in sorted(played, key=lambda i: g.at[i, "tip"]):
        for p in POINTS:
            t = point_time(p, g.at[gid, "tip"])
            pick = predict_game(store, design, fits, g.loc[gid], p, t)
            known = odds[(odds.game_id == gid) & (odds.known_at <= t)
                         & odds.spread_home.notna()].sort_values("known_at")  # fmt: skip
            line = None if known.empty else float(known.spread_home.iloc[-1])
            margin = float(res.at[gid, "margin"])
            rows.append({"game": f"{g.at[gid, 'away_abbr']}@{g.at[gid, 'home_abbr']}",
                         "tip": g.at[gid, "tip"].isoformat(), "point": p,
                         "model_margin": pick.margin, "market_margin": None if line is None
                         else -line, "result_margin": margin,
                         "model_err": abs(pick.margin - margin),
                         "market_err": None if line is None else abs(-line - margin)})
            # fmt: skip
    import pandas as pd

    df = pd.DataFrame(rows)
    summary = {"version": version, "games": len(played)}
    if len(df):
        for p in POINTS:
            s = df[df.point == p]
            m = s.market_err.notna()
            summary[p] = {"model_mae": round(float(s.model_err.mean()), 2),
                          "market_mae": round(float(s.market_err[m].mean()), 2) if m.any()
                          else None, "with_line": int(m.sum())}  # fmt: skip
    path = out or REPORTS / "m14_replay_2026.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"summary": summary, "games": rows}, indent=1, default=str))
    typer.echo(json.dumps(summary, indent=1))
    for r in rows:
        typer.echo(f"{r['tip'][:16]} {r['game']:>8} {r['point']:>7}  model {r['model_margin']:+.1f}"
                   f"  market {r['market_margin'] if r['market_margin'] is not None else '-'}"
                   f"  result {r['result_margin']:+.0f}")  # fmt: skip


@nba_app.command("status-changes")
def status_changes_cmd():
    """How often a player's official status changes between the morning and pre-tip points,
    per season (from every stored report version)."""
    import pandas as pd

    from betdata.nba.data import load_store
    from betdata.nba.points import morning_time, pretip_time

    _, engine = _engine()
    with engine.connect() as conn:
        store = load_store(conn)
    inj = store.frame("injuries").df
    g = store.games
    out = {}
    for season, games_s in g.groupby("season"):
        sub = inj[inj.game_id.isin(games_s.index)]
        if sub.empty:
            continue
        listed = changed = to_out = to_play = games_n = 0
        for gid, rows in sub.groupby("game_id"):
            tip = g.at[gid, "tip"]
            m, p = morning_time(tip), pretip_time(tip)
            if m >= p:
                continue
            games_n += 1
            at_m = rows[rows.known_at <= m].groupby("player_name").status.last()
            at_p = rows[rows.known_at <= p].groupby("player_name").status.last()
            for name, s_p in at_p.items():
                s_m = at_m.get(name)
                listed += 1
                if s_m != s_p:
                    changed += 1
                    to_out += s_p == "Out" and s_m in ("Questionable", "Doubtful", "Probable")
                    to_play += s_p in ("Available", "Removed") and s_m in (
                        "Questionable", "Doubtful", "Probable")
        out[int(season)] = {"games": games_n, "player_rows_at_pretip": listed,
                            "changed": changed,
                            "changed_share": round(changed / listed, 4) if listed else None,
                            "uncertain_to_out": int(to_out),
                            "uncertain_to_available": int(to_play)}  # fmt: skip
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "m14_status_changes.json").write_text(json.dumps(out, indent=1))
    typer.echo(pd.DataFrame(out).T.to_string())


@nba_app.command("bootstrap")
def bootstrap_cmd():
    """Load NBA history (schedules and box scores since 2015-16, this season's games so far,
    lineup stints for this season and the two before) into the database without a runner run:
    for adding NBA to an existing state, which is not a lost-state rebuild (the page's rebuild
    notice stays off). With history present, only missing lineup stints are loaded."""
    from betdata.db.schema import utcnow
    from betdata.runner.hourly import has_history
    from betdata.sports import load_entries, plugin

    _, engine = _engine()
    entry = next(e for e in load_entries() if e.key == "nba")
    sport = plugin(entry) if entry.plugin else None
    if sport is None:  # NBA not active yet: use the plugin class directly
        from betdata.sports.nba import NbaSport

        sport = NbaSport(entry)
    if has_history(engine, "nba"):
        typer.echo("NBA history present; loading lineup stints where missing")
        sport.load_stints(engine, utcnow())
        return
    sport.bootstrap(engine, utcnow())
    typer.echo(f"NBA history loaded: {has_history(engine, 'nba')}")


@nba_app.command("props-select")
def props_select_cmd(rebuild: bool = False, adjust: str = ""):
    """M15 NBA prop model on validation (2021-22, 2022-23): error vs the last-10 average per
    market and calibration. Writes reports/nba/props_selection*.json."""
    from betdata.nba.props.select import select

    res = select(rebuild, adjust or None)
    v = res["validation"]
    typer.echo(f"minutes MAE {v['minutes']['mae']:.3f} (last 10: {v['minutes']['mae_last10']:.3f})")
    for mk in ("points", "rebounds", "assists", "threes", "pra"):
        r = v[mk]
        typer.echo(f"{mk:9s} MAE {r['mae']:.3f} (last 10 {r['mae_last10']:.3f})  RMSE "
                   f"{r['rmse']:.3f} ({r['rmse_last10']:.3f})  Brier {r['brier_synthetic']:.4f}")


@nba_app.command("props-tune")
def props_tune_cmd():
    """Validation 2022-23 at the market's prop lines: calibration (one fit only if off) and an
    edge threshold per backtested market. Writes reports/nba/props_tune.json."""
    from betdata.nba.props.select import tune_lines

    _, engine = _engine()
    with engine.connect() as conn:
        rep = tune_lines(conn)
    for mk, r in rep["markets"].items():
        c = r.get("calibration") or {}
        typer.echo(f"{mk:9s} rows {r['validation_rows']:6d}  threshold {r.get('threshold')}  "
                   f"platt {c.get('platt_slope', float('nan')):.3f}/"
                   f"{c.get('platt_intercept', float('nan')):+.3f}  applied "
                   f"{r.get('calibration_applied')}")


@nba_app.command("props-final-test")
def props_final_test_cmd(again: bool = typer.Option(False, help="Score again (reproduce).")):
    """Score the NBA prop model once on 2023-24 to 2025-26 (reports/nba/props_final_test.json)."""
    from betdata.nba.props.select import final_test

    _, engine = _engine()
    with engine.connect() as conn:
        res = final_test(conn, again)
    typer.echo(json.dumps(res, indent=1, default=float)[:6000])


@nba_app.command("props-freeze")
def props_freeze_cmd():
    """Fit the prop model on every completed season and write the live fit and design."""
    from betdata.nba.props.select import freeze

    typer.echo(f"wrote {freeze()}")


@nba_app.command("select15")
def select15_cmd(rebuild: bool = False):
    """M15 side version nba15: add the M15 groups one at a time to the M14 frozen design on
    validation (2021-22, 2022-23). Writes reports/nba/nba15_selection.json."""
    from betdata.nba.select15 import select15

    rep = select15(rebuild)
    for s in rep["steps"]:
        typer.echo(f"{s['step']:>18}  margin {s['margin']:.3f}  total {s['total']:.3f}  "
                   f"{'kept' if s['kept'] else 'dropped'}")
    for a in rep["ablation"]:
        typer.echo(f" without {a['without']:>6}: margin {a['margin']:.3f} total {a['total']:.3f}")


@nba_app.command("final-test15")
def final_test15_cmd(again: bool = typer.Option(False, help="Score again (reproduce only).")):
    """nba15 on 2023-24 to 2025-26 (second look), once; decides the page switch."""
    from betdata.nba.select15 import final_test15

    typer.echo(json.dumps(final_test15(again), indent=1, default=float))


@nba_app.command("freeze15")
def freeze15_cmd():
    """Fit nba15 on 2015-2025 and write config/fits/nba15_fit_2015_2025.json."""
    from betdata.nba.select15 import freeze15

    typer.echo(f"wrote {freeze15()}")
