"""The live m15 NFL side version: the frozen m15 fit (config/fits/, written once by
`betdata nfl freeze15`, never refitted hourly) predicting the same games and points as m13w,
stored change-only as model "m15" and graded live. The page shows it only if
config/nfl_model_m15.json says `page: true` (M15 switch rule); otherwise it runs in the
background. m12 and m13w are predicted exactly as before, by nfl/live.py.

Its M15 inputs are rebuilt from the files on disk each run (only the last seasons), with the
expected-rate fits frozen in the fit file, so a live feature uses the same expectations as
the fit."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine

from betdata.config import PROJECT_ROOT
from betdata.core.matching import season_for
from betdata.nfl.m15 import DESIGN15_PATH, groups_of, load_design15
from betdata.nfl.model import Ridge
from betdata.nfl.pipeline import TRAIN, Fit, outcomes
from betdata.nfl.points import point_time
from betdata.nfl.predict import LivePick, market_weights, predict_games, store_picks
from betdata.nfl.selection import fit_design

MODEL = "m15"
LIVE_SEASONS = 3  # seasons of M15 inputs rebuilt live (the rates look back 600 days)
MIN_LAST_SEASON_ROWS = 400  # team-games of last season required (a season has ~570)


def fit_name(raw_design: dict, seasons) -> str:
    keep = {k: raw_design[k] for k in ("margin_groups", "total_groups", "lam_margin",
                                       "lam_total", "m15_group_versions") if k in raw_design}
    tag = hashlib.sha1(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:10]
    return f"nfl_m15_fit_{tag}_{min(seasons)}_{max(seasons)}.json"


def freeze(store, info: dict, now: datetime, fits_dir: Path | None = None,
           design_path: Path | None = None) -> Path:  # fmt: skip
    """Fit the m15 design on every completed season from 2013 to a new file under
    config/fits/ (refuses to overwrite one) and record it in the design file as `fit`."""
    from betdata.nfl.m15 import rows15, save_meta

    design_path = design_path or DESIGN15_PATH
    design, meta = load_design15(design_path)
    seasons = tuple(range(2013, season_for("nfl", now)))
    raw_design = json.loads(design_path.read_text())
    path = (fits_dir or PROJECT_ROOT / "config" / "fits") / fit_name(raw_design, seasons)
    if path.exists():
        raise FileExistsError(f"{path} exists; committed fits are never overwritten")
    rows = rows15(store, design, meta, seasons)
    fits = fit_design(rows, outcomes(store), design, seasons)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "seasons": list(seasons), "expected": info["expected"],
        "fits": {p: {"margin": f.margin.to_dict(), "total": f.total.to_dict(),
                     "sigma": f.sigma} for p, f in fits.items()}}) + "\n")  # fmt: skip
    rel = path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path
    save_meta({"fit": str(rel)}, design_path)
    return path


def load15(design_path: Path | None = None):
    """(design, meta, fits, expected, version), or None while no frozen fit exists."""
    design_path = design_path or DESIGN15_PATH
    if not design_path.exists():
        return None
    design, meta = load_design15(design_path)
    if not meta.get("fit"):
        return None
    text = (PROJECT_ROOT / meta["fit"]).read_text()
    raw = json.loads(text)
    fits = {p: Fit(p, Ridge.from_dict(f["margin"]), Ridge.from_dict(f["total"]), f["sigma"])
            for p, f in raw["fits"].items()}  # fmt: skip
    version = f"{MODEL}:{hashlib.sha1(text.encode()).hexdigest()[:10]}"
    return design, meta, fits, raw["expected"], version


def version15() -> str | None:
    got = load15()
    return got[4] if got else None


def page15() -> bool:
    got = load15()
    return bool(got and got[1].get("page"))


def inputs_ready(store, now: datetime) -> None:
    """Refuse (ValueError) to predict from incomplete M15 inputs, so a machine without the
    play-by-play and nflverse files never stores m15 numbers built on empty rates: the last
    season's games, at least 90% of this season's finished games, and a depth chart from the
    last 10 days (in season) must be there."""
    import pandas as pd

    t = pd.Timestamp(now)
    tg, res, g = store.frame("m15_tg").df, store.frame("results").df, store.games
    season = season_for("nfl", now)
    if (tg.season == season - 1).sum() < MIN_LAST_SEASON_ROWS:
        raise ValueError(f"m15 inputs: play-by-play for {season - 1} missing")
    done = set(res.game_id[res.known_at <= t]) & set(g.index[g.season == season])
    have = set(tg.game_id[tg.known_at <= t])
    if done and len(done & have) < 0.9 * len(done):
        raise ValueError(f"m15 inputs: {len(done - have)} of {len(done)} {season} games missing")
    depth = store.frame("m15_depth").df
    if done and (depth.empty or depth.known_at[depth.known_at <= t].max()
                 < t - pd.Timedelta(days=10)):  # fmt: skip
        raise ValueError("m15 inputs: no depth chart from the last 10 days")


def run_predict15(engine: Engine, now: datetime, days: int) -> list[LivePick]:
    from betdata.nfl.live import attach_moneylines, loaded_store, missed_points
    from betdata.nfl.m15data import Expected, with_m15
    from betdata.nfl.workflow import data_dir

    got = load15()
    if got is None:
        return []
    design, meta, fits, expected, version = got
    store = loaded_store(engine)
    with engine.connect() as conn:
        store, _ = with_m15(store, conn, data_dir(), TRAIN, expected=Expected.from_dict(expected),
                            first_season=season_for("nfl", now) - LIVE_SEASONS)  # fmt: skip
    inputs_ready(store, now)
    g = store.games
    upcoming = [i for i in g.index if now < g.at[i, "kickoff"] <= now + timedelta(days=days)
                and g.at[i, "status"] == "scheduled"]  # fmt: skip
    groups, weights = groups_of(design, meta), market_weights(design)
    with engine.begin() as conn:
        for gid, point in missed_points(conn, store, version, upcoming, now):
            t = point_time(point, g.at[gid, "kickoff"])
            back = predict_games(store, design, fits, groups, [gid], t, weights)
            attach_moneylines(conn, back, t)
            store_picks(conn, back, version, t, model=MODEL)
    picks = predict_games(store, design, fits, groups, upcoming, now, weights)
    with engine.begin() as conn:
        attach_moneylines(conn, picks, now)
        store_picks(conn, picks, version, now, model=MODEL)
    return picks
