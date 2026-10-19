"""Live runs of the M12 model: `betdata predict nfl` and the replay of the live weeks."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine

from betdata.core.matching import season_for
from betdata.nfl.data import load_store
from betdata.nfl.groups import GROUPS
from betdata.nfl.pipeline import outcomes
from betdata.nfl.points import POINTS, point_time
from betdata.nfl.predict import (
    LivePick,
    market_weights,
    predict_games,
    store_picks,
    version_of,
)
from betdata.nfl.selection import Design, fit_design
from betdata.nfl.workflow import design_rows, fitted_params


def require_history(store) -> None:
    have = set(store.frame("results").df.game_id) & set(store.frame("team_games").df.game_id)
    if len(have) < 500:
        raise ValueError(
            "not enough NFL history to fit the M12 model: load nflverse schedules, team_stats, "
            "snaps, injuries, rosters and pbp (`betdata backfill --source nflverse ...`), then "
            "run `betdata nfl derive`"
        )


def loaded_store(engine: Engine):
    with engine.connect() as conn:
        store = load_store(conn)
    require_history(store)
    return fitted_params(store)


def live_model(store, design: Design, now: datetime):
    """The frozen design refitted on every completed season from 2013."""
    last = season_for("nfl", now) - 1
    seasons = tuple(range(2013, last + 1))
    rows = design_rows(store, design, seasons)
    fits = fit_design(rows, outcomes(store), design, seasons)
    return fits, seasons


def run_predict(engine: Engine, now: datetime, days: int) -> tuple[str, list[LivePick]]:
    design = Design.load()
    store = loaded_store(engine)
    fits, _ = live_model(store, design, now)
    version = version_of(design, fits)
    g = store.games
    upcoming = [i for i in g.index
                if now < g.at[i, "kickoff"] <= now + timedelta(days=days)
                and g.at[i, "status"] == "scheduled"]  # fmt: skip
    groups = [GROUPS[n] for n in design.groups]
    picks = predict_games(store, design, fits, groups, upcoming, now, market_weights(design))
    with engine.begin() as conn:
        attach_moneylines(conn, picks, now)
        store_picks(conn, picks, version, now)
    return version, picks


def attach_moneylines(conn, picks: list[LivePick], now: datetime) -> None:
    """The latest polled moneyline (both sides, one book) known at `now`, for each pick."""
    from betdata.quotes import current_quotes

    quotes = current_quotes(conn, [p.game_id for p in picks], now)
    for p in picks:
        q = quotes.get(p.game_id, {})
        if "ml_home" in q:
            p.ml_home, p.ml_away = q["ml_home"].price, q["ml_away"].price


def replay(engine: Engine, csv_paths: list[Path], now: datetime) -> list[dict]:
    """What the M12 model would have said at each prediction point of the games in the live
    CSVs (the live model, fitted on seasons before the CSVs' season), next to M10/M11's logged
    numbers and the result. Market numbers are the ones logged in the CSVs (no line history
    for those days is stored)."""
    from betdata.nfl.final import kickoff_key, read_live_csvs

    logged = read_live_csvs(csv_paths)
    design = Design.load()
    store = loaded_store(engine)
    fits, _ = live_model(store, design, now)
    out = outcomes(store)
    groups = [GROUPS[n] for n in design.groups]
    g = store.games
    names = store.names
    rows = []
    for gid in g.index:
        key = (kickoff_key(g.at[gid, "kickoff"]), names.get(f"team:{g.at[gid, 'away']}"),
               names.get(f"team:{g.at[gid, 'home']}"))  # fmt: skip
        if key not in logged:
            continue
        rec = {"game_id": int(gid), "kickoff": key[0], "away": key[1], "home": key[2],
               **logged[key]}  # fmt: skip
        for p in POINTS:
            t = point_time(p, g.at[gid, "kickoff"])
            [pick] = predict_games(store, design, fits, groups, [gid], t,
                                   market_weights(design))  # fmt: skip
            rec[f"{p}_spread"] = -pick.margin
            rec[f"{p}_total"] = pick.total
            rec[f"{p}_factors"] = pick.margin_factors[:3]
            rec[f"{p}_missing"] = pick.missing
        if gid in out.index:
            rec["result_margin"] = float(out.at[gid, "margin"])
            rec["result_total"] = float(out.at[gid, "total"])
        rows.append(rec)
    return sorted(rows, key=lambda r: (r["kickoff"], r["home"]))
