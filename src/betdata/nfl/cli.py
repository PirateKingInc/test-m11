"""`betdata nfl ...`: M12 data maintenance and checks."""

from __future__ import annotations

import typer

from betdata.nfl.workflow import (
    close_mae,
    closing_lines,
    data_dir,
    design_rows,
    feature_rows,
    fitted_params,
    model_games,
)

nfl_app = typer.Typer(help="NFL model (M12): as-of data, checks.", no_args_is_help=True)


def _engine():
    from betdata.config import get_settings
    from betdata.db.engine import make_engine

    return make_engine(get_settings().db_url)


@nfl_app.command("backfill-report-dates")
def backfill_report_dates():
    """Fill injury publish times (nflverse date_modified) on rows loaded before they were
    parsed, from the archived raw files. No network call; safe to repeat."""
    from betdata.sources.nflverse_data import fill_report_dates

    with _engine().begin() as conn:
        updated, files = fill_report_dates(conn)
    typer.echo(f"read {files} archived injury files; filled report_date on {updated} rows")




@nfl_app.command("derive")
def derive(
    seasons: str = typer.Option(None, help="pbp seasons to rebuild, e.g. 2025 (default: all)"),
):
    """Rebuild derived tables from files already stored: game info from the archived
    schedule, play-by-play aggregates from data/pbp. No network call."""
    from betdata.nfl.derive import derive_game_info, derive_pbp
    from betdata.sources.nflverse import parse_seasons

    years = sorted(parse_seasons(seasons) or []) if seasons else None
    with _engine().begin() as conn:
        n_info = derive_game_info(conn)
        n_pbp = derive_pbp(conn, data_dir(), years)
    typer.echo(f"game info: {n_info} games; play-by-play: {n_pbp} team/player rows")


def _groups(names: str | None):
    from betdata.nfl.groups import GROUPS, resolve

    return resolve(names) if names else list(GROUPS.values())


@nfl_app.command("asof-check")
def asof_check(
    sample: int = typer.Option(40, help="Games to check (spread over seasons)."),
    groups: str = typer.Option(None, help="Comma-separated groups (default: all)."),
    seed: int = typer.Option(0),
):
    """Rebuild features as of each prediction point from (a) all data, (b) only rows known by
    then, (c) all data with later rows scrambled; fail if any value differs."""
    import random

    from betdata.nfl.asof import check
    from betdata.nfl.data import load_store

    with _engine().connect() as conn:
        store = fitted_params(load_store(conn))
    ids = [i for i in store.games.index if store.games.at[i, "season"] >= 2013]
    rng = random.Random(seed)
    picked = sorted(rng.sample(ids, min(sample, len(ids))))
    bad = check(store, _groups(groups), picked, seed=seed)
    for v in bad[:20]:
        typer.echo(f"LOOK-AHEAD game {v.game_id} {v.point} ({v.variant}): {', '.join(v.features)}")
    typer.echo(f"checked {len(picked)} games x 2 points: {len(bad)} violations")
    if bad:
        raise typer.Exit(1)












@nfl_app.command("evaluate")
def evaluate_cmd(
    groups: str = typer.Option(None, help="Groups to add in order (default: all built)."),
    refresh: bool = typer.Option(False, help="Recompute cached feature rows."),
    write_design: bool = typer.Option(
        False, help="Save the kept groups and penalties to config/nfl_model.json."
    ),
):
    """Add feature groups one at a time on 2013-2019 (fit) / 2020-2022 (validate) and report
    validation MAE. Test seasons (2023-2025) are never touched here."""
    from betdata.nfl.data import load_store
    from betdata.nfl.groups import GROUPS as ALL
    from betdata.nfl.groups import ORDER, columns
    from betdata.nfl.pipeline import TRAIN, VALIDATE, evaluate, outcomes

    names = [n.strip() for n in groups.split(",")] if groups else ORDER
    if "base" not in names:
        names = ["base", *names]
    with _engine().connect() as conn:
        store = load_store(conn)
        close = closing_lines(conn)
    store = fitted_params(store)
    ids = model_games(store, (*TRAIN, *VALIDATE))
    out = outcomes(store)
    vids = [i for i in ids if store.games.at[i, "season"] in VALIDATE]
    cm, ct = close_mae(close, out, vids)
    typer.echo(f"{'closing line (reference)':28} {cm:8.3f} {ct:8.3f}  (validation games)")
    rows = None
    kept_m, kept_t = [], []
    groups_m, groups_t = [], []
    best_m = best_t = None
    typer.echo(f"{'step':28} {'margin':>8} {'total':>8}  {'lam':>9}  decision (margin / total)")
    for name in names:
        r = feature_rows(store, ALL[name], ids, refresh)
        rows = r if rows is None else rows.merge(
            r.drop(columns=["season", "week"]), on=["game_id", "point"])  # fmt: skip
        m, t = columns(rows.columns, name)
        ev = evaluate(rows, out, kept_m + m or ["m_base_hfa"], kept_t + t or ["t_base_league"])
        if best_m is None:
            keep_m = keep_t = True
            decision = "base"
        else:
            keep_m = bool(m) and ev.margin < best_m
            keep_t = bool(t) and ev.total < best_t
            decision = (f"{'KEEP' if keep_m else 'drop' if m else '-'} / "
                        f"{'KEEP' if keep_t else 'drop' if t else '-'}")  # fmt: skip
        typer.echo(f"{'+ ' + name:28} {ev.margin:8.3f} {ev.total:8.3f}  "
                   f"{ev.lam_m:4g}/{ev.lam_t:<4g}  {decision}")  # fmt: skip
        for p, (em, et, n) in ev.valid.items():
            typer.echo(f"    {p:14} valid {em:6.3f} {et:6.3f} ({n} games)   "
                       f"train {ev.train[p][0]:6.3f} {ev.train[p][1]:6.3f}")  # fmt: skip
        if keep_m:
            kept_m, best_m = kept_m + m, ev.margin
            groups_m.append(name)
        if keep_t:
            kept_t, best_t = kept_t + t, ev.total
            groups_t.append(name)
    final = evaluate(rows, out, kept_m, kept_t)
    typer.echo(f"kept: margin {groups_m}, total {groups_t}; validation MAE "
               f"{final.margin:.3f} / {final.total:.3f}, penalties {final.lam_m:g} / "
               f"{final.lam_t:g}")  # fmt: skip
    if write_design:
        from betdata.nfl.selection import DESIGN_PATH, Design

        Design(groups_m, groups_t, final.lam_m, final.lam_t).save()
        typer.echo(f"design written to {DESIGN_PATH}")




@nfl_app.command("select")
def select_cmd(
    write: bool = typer.Option(False, help="Save the model choice and blend weights."),
):
    """On validation seasons only: ridge vs gradient boosting, win-probability calibration,
    the ablation table (each kept group removed in turn), and the model+market blend weight."""
    import numpy as np

    from betdata.nfl.data import load_store
    from betdata.nfl.pipeline import TRAIN, VALIDATE, evaluate, outcomes
    from betdata.nfl.points import POINTS
    from betdata.nfl.selection import Design, calibration, fit_design, gbm_mae, predict_rows

    design = Design.load()
    with _engine().connect() as conn:
        store = fitted_params(load_store(conn))
        close = closing_lines(conn)
    rows = design_rows(store, design, (*TRAIN, *VALIDATE))
    out = outcomes(store)
    m_cols, t_cols = design.columns(rows.columns)
    ridge = evaluate(rows, out, m_cols, t_cols, lambdas=(design.lam_margin,),
                     lambdas_t=(design.lam_total,))
    gbm = gbm_mae(rows, out, design, TRAIN, VALIDATE)
    typer.echo("Model choice (validation 2020-2022 MAE, margin / total):")
    for p in POINTS:
        typer.echo(f"  {p:14} ridge {ridge.valid[p][0]:.3f} / {ridge.valid[p][1]:.3f}   "
                   f"gbm {gbm[p][0]:.3f} / {gbm[p][1]:.3f}")  # fmt: skip
    g_m = float(np.mean([v[0] for v in gbm.values()]))
    g_t = float(np.mean([v[1] for v in gbm.values()]))
    clearly = g_m < ridge.margin - 0.05 and g_t < ridge.total - 0.05
    design.model = "gbm" if clearly else "ridge"
    typer.echo(f"  mean: ridge {ridge.margin:.3f} / {ridge.total:.3f}, gbm {g_m:.3f} / {g_t:.3f}"
               f" -> {design.model} (gbm must win both by 0.05 to replace the simpler model)")

    fits = fit_design(rows, out, design, TRAIN)
    vrows = rows[rows.season.isin(VALIDATE)]
    pred = predict_rows(vrows, fits)
    typer.echo("Calibration of P(home wins) on validation:")
    for p in POINTS:
        c = calibration(pred[pred.point == p], out)
        typer.echo(f"  {p:14} Brier {c['brier']:.4f}  log loss {c['logloss']:.4f}  "
                   f"({c['n']} games, sigma {fits[p].sigma:.2f})")  # fmt: skip
        for lo, hi, n, mp, obs in c["bins"]:
            typer.echo(f"      {lo:.1f}-{hi:.1f}: {n:4} games, predicted {mp:.3f}, won {obs:.3f}")

    typer.echo("Ablation (validation MAE with each kept group removed):")
    typer.echo(f"  {'all kept':24} {ridge.margin:.3f} / {ridge.total:.3f}")
    for g in design.groups:
        if g == "base":
            continue
        d2 = Design([x for x in design.margin_groups if x != g],
                    [x for x in design.total_groups if x != g], design.lam_margin,
                    design.lam_total)  # fmt: skip
        mc, tc = d2.columns(rows.columns)
        e = evaluate(rows, out, mc, tc, lambdas=(design.lam_margin,),
                     lambdas_t=(design.lam_total,))
        typer.echo(f"  {'without ' + g:24} {e.margin:.3f} / {e.total:.3f}   "
                   f"(+{e.margin - ridge.margin:.3f} / +{e.total - ridge.total:.3f})")

    # Blend weight: the closing line stands in for the prediction-time line (history has no
    # mid-week lines). It only picks one number per market; it is never a model input.
    pv = pred[pred.point == "prekick"].set_index("game_id")
    ids = [i for i in pv.index if close.get(i, (None, None))[0] is not None and i in out.index]
    best = {}
    for target, col, mk in (("margin", "margin", lambda i: -close[i][0]),
                            ("total", "total", lambda i: close[i][1])):  # fmt: skip
        y = out.loc[ids, target].to_numpy()
        model = pv.loc[ids, col].to_numpy()
        market = np.array([mk(i) for i in ids], dtype=float)
        errs = {w: float(np.abs(w * model + (1 - w) * market - y).mean())
                for w in np.round(np.arange(0, 1.01, 0.05), 2)}  # fmt: skip
        w = min(errs, key=errs.get)
        best[target] = float(w)
        typer.echo(f"Blend {target}: model weight {w:.2f} (validation MAE {errs[w]:.3f}; "
                   f"model alone {errs[1.0]:.3f}, market alone {errs[0.0]:.3f})")
    design.blend_margin, design.blend_total = best["margin"], best["total"]
    if write:
        design.save()
        typer.echo("design updated")


@nfl_app.command("final-test")
def final_test_cmd(force: bool = typer.Option(False, help="Score the test seasons again.")):
    """Score 2023-2025 once: M12 (refitted on 2013-2022) vs Elo, M11 and the closing line."""
    from betdata.analysis.context import load_context
    from betdata.analysis.dataset import load_games
    from betdata.analysis.units_backtest import run_units_backtest
    from betdata.nfl.data import load_store
    from betdata.nfl.final import final_test
    from betdata.nfl.pipeline import TEST, TRAIN, VALIDATE, outcomes
    from betdata.nfl.selection import Design

    path = data_dir() / "reports" / "m12_final_test.json"
    if path.exists() and not force:
        typer.echo(f"test seasons already scored: {path} (they are scored once)")
        raise typer.Exit(1)
    design = Design.load()
    with _engine().connect() as conn:
        store = fitted_params(load_store(conn))
        games = load_games(conn, "nfl")
        ctx = load_context(conn)
    units = run_units_backtest(games, ctx, list(TRAIN), [2020, 2021, 2022, *TEST])
    units_preds = {g.game_id: p for g, p in units.preds["units+missing"]}
    rows = design_rows(store, design, (*TRAIN, *VALIDATE, *TEST))
    report = final_test(rows, outcomes(store), design, games, units_preds, path, force=force)
    for point, res in report["points"].items():
        typer.echo(f"{point} ({res['games']} games)")
        typer.echo("  margin MAE: " + ", ".join(f"{k} {v:.3f}" for k, v in
                                                res["margin_mae"].items()))  # fmt: skip
        typer.echo("  total MAE:  " + ", ".join(f"{k} {v:.3f}" for k, v in
                                                res["total_mae"].items()))  # fmt: skip
        c = res["calibration"]
        typer.echo(f"  calibration: Brier {c['brier']:.4f}, log loss {c['logloss']:.4f}")
        for b in res["bets"].values():
            ci = b["ci95"]
            rng = "-" if ci is None else f"{100 * ci[0]:.1f}-{100 * ci[1]:.1f}%"
            roi = "-" if b["roi"] is None else f"{100 * b['roi']:+.1f}%"
            typer.echo(f"  bets {b['label']:12} {b['record']:>12}  {100 * b['win_rate']:.1f}%  "
                       f"95% {rng}  ROI {roi}")
    typer.echo(f"report: {path}")


@nfl_app.command("replay")
def replay_cmd():
    """Replay the live weeks in live/*.csv: what M12 would have said at each prediction point,
    next to what M10 (Elo) and M11 (units) logged and what happened."""
    from betdata.config import PROJECT_ROOT
    from betdata.db.schema import utcnow
    from betdata.nfl.live import replay

    paths = sorted((PROJECT_ROOT / "live").glob("*.csv"))
    rows = replay(_engine(), paths, utcnow())
    head = (f"{'kickoff (UTC)':16} {'game':11} {'M10':>6} {'M11':>6} {'M12 fr':>7} "
            f"{'M12 pk':>7} {'mkt':>6} {'result':>7} | {'M11 tot':>7} {'M12 tot':>7} "
            f"{'mkt tot':>7} {'actual':>6}")  # fmt: skip
    lines = [head]

    def f(x):
        return "-" if x is None else f"{x:+.1f}"

    def g(x):
        return "-" if x is None else f"{x:.1f}"

    for r in rows:
        res = r.get("result_margin")
        lines.append(
            f"{r['kickoff']:16} {r['away'] + '@' + r['home']:11} {f(r.get('elo_spread')):>6} "
            f"{f(r.get('m11_spread')):>6} {f(r['final_report_spread']):>7} "
            f"{f(r['prekick_spread']):>7} {f(r.get('m11_market', r.get('elo_market'))):>6} "
            f"{f(None if res is None else -res):>7} | {g(r.get('m11_total')):>7} "
            f"{g(r['prekick_total']):>7} {g(r.get('m11_market_total')):>7} "
            f"{g(r.get('result_total')):>6}"
        )  # fmt: skip
    lines.append("Spreads are the home side (negative = home favoured); result = minus the home "
                 "margin, on the same scale. mkt = the line logged with the live picks.")
    for r in rows:
        if r.get("prekick_missing") and any(r["prekick_missing"].values()):
            miss = "; ".join(f"{k}: {', '.join(v)}" for k, v in r["prekick_missing"].items() if v)
            lines.append(f"  {r['away']}@{r['home']} out at prekick: {miss}")
    typer.echo("\n".join(lines))
    path = data_dir() / "reports" / "m12_replay.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    typer.echo(f"written to {path}")
