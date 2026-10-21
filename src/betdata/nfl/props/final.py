"""The one scoring of the NFL prop model on the test seasons (2023-2025) and the frozen fit.

final_test: the design decided on validation (reports/nfl/props_selection.json) refitted on
2013-2022 and scored once on 2023-2025: error vs the naive baseline, calibration at the
synthetic line, and, where prop lines are stored for test games (table prop_lines: ESPN BET
2024-2025, loaded by the props-history backfill), the error and P(over) against the real line.
There is no betting backtest (no lines in validation to choose thresholds on). A second run
refuses unless `again`.

freeze: the same design fitted on every completed season (2013-2025), with M12's play
probabilities, to config/fits/nfl_props_<range>.json; config/nfl_props.json names it, with
every market research-only (no thresholds, no test ROI).
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
from sqlalchemy import Connection, select

from betdata.config import PROJECT_ROOT
from betdata.db.schema import games, prop_lines
from betdata.moneyline import no_vig
from betdata.nfl.pipeline import TEST, TRAIN, VALIDATE
from betdata.nfl.points import PREKICK
from betdata.nfl.props import model as M
from betdata.nfl.props.history import history_features
from betdata.nfl.props.select import (
    FINAL,
    REPORTS,
    calibration,
    chosen,
    fit_frozen,
    score_market,
    split,
    synthetic_line,
)

DESIGN_PATH = PROJECT_ROOT / "config" / "nfl_props.json"
FIT_DIR = PROJECT_ROOT / "config" / "fits"


def final_test(conn: Connection, again: bool = False) -> dict:
    if FINAL.exists() and not again:
        raise RuntimeError(f"test seasons already scored ({FINAL}); they are scored once "
                           "(--again to repeat)")  # fmt: skip
    f = history_features()
    dec = chosen()
    mod = fit_frozen(f, (*TRAIN, *VALIDATE), dec)
    te = split(f, TEST)
    pops = M.populations(te)
    res = {"fit_seasons": [*TRAIN, *VALIDATE], "test": list(TEST), "decisions": dec,
           "no_betting_backtest": "no NFL prop lines exist for the validation seasons "
                                  "(2020-2022), so no edge threshold can be chosen",
           "markets": {}}  # fmt: skip
    for mk in M.MARKETS:
        d = te[pops[mk]]
        r = score_market(mod, d, mk)
        p = mod.p_over(mk, mod.predict(d, mk), synthetic_line(d, mk))
        hit = (d[f"y_{M.MARKETS[mk]}"].to_numpy() > synthetic_line(d, mk)).astype(float)
        r["brier_final"] = float(np.mean((p - hit) ** 2))
        r["calibration_final"] = calibration(p, hit)
        r["by_season"] = {int(s): {"n": int(len(g)), "mae": float(np.mean(np.abs(
            mod.predict(g, mk) - g[f"y_{M.MARKETS[mk]}"]))), "mae_baseline": float(np.mean(
            np.abs(g[f"g10_{M.MARKETS[mk]}" if mk != "anytime_td" else "sn_td"].fillna(0)
                   - g[f"y_{M.MARKETS[mk]}"])))} for s, g in d.groupby("season")}  # fmt: skip
        res["markets"][mk] = r
    res["vs_lines"] = line_comparison(conn, te, mod)
    REPORTS.mkdir(parents=True, exist_ok=True)
    FINAL.write_text(json.dumps(res, indent=1, default=float) + "\n")
    return res


def closing_prop_lines(conn: Connection, game_ids) -> pd.DataFrame:
    """Per (game, ESPN player, market): the stored closing line (kind close), else the last
    polled line before kickoff."""
    q = (select(prop_lines.c.game_id, prop_lines.c.player_id, prop_lines.c.player_name,
                prop_lines.c.market, prop_lines.c.kind, prop_lines.c.line,
                prop_lines.c.over_price, prop_lines.c.under_price, prop_lines.c.fetched_at,
                games.c.start_time_utc)
         .join(games, games.c.game_id == prop_lines.c.game_id)
         .where(prop_lines.c.game_id.in_([int(g) for g in game_ids]),
                prop_lines.c.market.in_(list(M.MARKETS))))  # fmt: skip
    df = pd.read_sql(q, conn)
    if df.empty:
        return df
    df["fetched_at"] = pd.to_datetime(df.fetched_at, utc=True)
    df["start_time_utc"] = pd.to_datetime(df.start_time_utc, utc=True)
    df = df[(df.kind == "close") | ((df.kind == "poll") & (df.fetched_at < df.start_time_utc))]
    df["rank"] = (df.kind != "close").astype(int)
    df = df.sort_values(["rank", "fetched_at"], ascending=[True, False])
    return df.drop_duplicates(["game_id", "player_id", "market"]).reset_index(drop=True)


def line_comparison(conn: Connection, te: pd.DataFrame, mod: M.PropModel) -> dict:
    """Model and line against the actual, on test player-games with a stored line (prekick
    prediction)."""
    from betdata.nfl.nflverse_files import load_player_stats
    from betdata.nfl.props.ids import espn_to_gsis
    from betdata.nfl.workflow import data_dir

    lines = closing_prop_lines(conn, te.game_id.unique())
    if lines.empty:
        return {"status": "no lines loaded"}
    stats = load_player_stats(TEST, columns=["player_id", "player_display_name"],
                              data_dir=data_dir()).rename(columns={"player_display_name": "name"})
    lines["gsis"] = espn_to_gsis(lines, data_dir(), (2025, 2026), stats)
    out = {"status": "ok", "lines": int(len(lines)),
           "mapped_to_gsis": float(lines.gsis.notna().mean())}  # fmt: skip
    pk = te[te.point == PREKICK].set_index(["game_id", "player_id"])
    pk = pk[~pk.index.duplicated()]
    for mk, ln in lines.dropna(subset=["gsis"]).groupby("market"):
        keys = pd.MultiIndex.from_arrays([ln.game_id, ln.gsis])
        ok = keys.isin(pk.index)
        ln, keys = ln[ok], keys[ok]
        if ln.empty:
            continue
        d = pk.loc[keys].reset_index()
        y = d[f"y_{M.MARKETS[mk]}"].to_numpy(dtype=float)
        line = ln.line.fillna(0.5).to_numpy(dtype=float)
        mu = mod.predict(d, mk)
        p = mod.p_over(mk, mu, line)
        over = (y > line).astype(float)
        push = y == line
        nv = [no_vig(o, u)[0] if pd.notna(o) and pd.notna(u) else np.nan
              for o, u in zip(ln.over_price, ln.under_price, strict=True)]  # fmt: skip
        nv = np.asarray(nv, dtype=float)
        has = ~np.isnan(nv) & ~push
        out[mk] = {"n": int(len(y)), "mae_model": float(np.mean(np.abs(mu - y))),
                   "mae_line": float(np.mean(np.abs(line - y))) if mk != "anytime_td" else None,
                   "model_side_hit_rate": float(np.mean(((p > 0.5) == (over > 0))[~push])),
                   "brier_model": float(np.mean((p[~push] - over[~push]) ** 2)),
                   "brier_no_vig_line": float(np.mean((nv[has] - over[has]) ** 2))
                   if has.any() else None, "n_priced": int(has.sum())}  # fmt: skip
    return out


def freeze() -> dict:
    """Fit on every completed season and write the frozen fit and the design."""
    f = history_features()
    dec = chosen()
    seasons = (*TRAIN, *VALIDATE, *TEST)
    mod = fit_frozen(f, seasons, dec)
    fit_path = FIT_DIR / f"nfl_props_{min(seasons)}_{max(seasons)}.json"
    FIT_DIR.mkdir(parents=True, exist_ok=True)
    fit_path.write_text(json.dumps({"seasons": list(seasons), "model": mod.to_dict(),
                                    "play_params": play_params(),
                                    "decisions": {k: v for k, v in dec.items()}},
                                   indent=1, default=float) + "\n")  # fmt: skip
    design = {"fit": str(fit_path.relative_to(PROJECT_ROOT)), "thresholds": {}, "test_roi": {},
              "research_only": list(M.MARKETS)}  # fmt: skip
    DESIGN_PATH.write_text(json.dumps(design, indent=2) + "\n")
    return design


def play_params() -> dict:
    """M12's play probabilities by status and replacement QB (learned on 2013-2019)."""
    from betdata.nfl.workflow import data_dir

    path = data_dir() / "derived" / f"params_{min(TRAIN)}_{max(TRAIN)}.json"
    if not path.exists():
        from betdata.nfl.cli import _engine
        from betdata.nfl.data import load_store
        from betdata.nfl.workflow import fitted_params

        with _engine().connect() as conn:
            fitted_params(load_store(conn))
    p = json.loads(path.read_text())
    return {"play_prob": p["play_prob"], "qb_repl": p["qb_repl"]}
