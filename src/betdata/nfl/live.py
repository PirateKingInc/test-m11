"""Live runs of the M12 model: `betdata predict nfl` and the replay of the live weeks."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine

from betdata.config import PROJECT_ROOT
from betdata.core.matching import season_for
from betdata.nfl.data import load_store
from betdata.nfl.groups import GROUPS
from betdata.nfl.model import Ridge
from betdata.nfl.pipeline import Fit, outcomes
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


def fit_path(design: Design, seasons) -> Path:
    from betdata.nfl.workflow import data_dir

    tag = hashlib.sha1(json.dumps(design.__dict__, sort_keys=True).encode()).hexdigest()[:10]
    return data_dir() / "derived" / f"live_fit_{tag}_{min(seasons)}_{max(seasons)}.json"


def frozen_model(store, design: Design, now: datetime):
    """The live model without refitting: the fit on every completed season is computed once
    (the first prediction of a season, or after the data directory is rebuilt) and read back
    from data/derived afterwards, so hourly runs never refit. The completed seasons only change
    once a year, so this is the same fit `live_model` would return. A fit committed under
    config/fits/ (the 2013-2025 fit M12 went live with) takes precedence, so a rebuilt data
    directory keeps exactly the same coefficients."""
    last = season_for("nfl", now) - 1
    seasons = tuple(range(2013, last + 1))
    path = fit_path(design, seasons)
    committed = PROJECT_ROOT / "config" / "fits" / path.name
    if committed.exists():  # the fit M12 shipped with: identical whatever data was rebuilt
        path = committed
    if path.exists():
        raw = json.loads(path.read_text())
        fits = {p: Fit(p, Ridge.from_dict(f["margin"]), Ridge.from_dict(f["total"]), f["sigma"])
                for p, f in raw["fits"].items()}  # fmt: skip
        return fits, seasons
    fits, seasons = live_model(store, design, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"seasons": list(seasons), "fits": {
        p: {"margin": f.margin.to_dict(), "total": f.total.to_dict(), "sigma": f.sigma}
        for p, f in fits.items()}}))  # fmt: skip
    return fits, seasons


def live_models() -> list[tuple[str, Design]]:
    """The model versions predicted live: frozen M12, and M13's weather version once validation
    accepted it (config/nfl_model_wx.json)."""
    from betdata.nfl.weather import WX_DESIGN_PATH

    out = [("m12", Design.load())]
    if WX_DESIGN_PATH.exists():
        out.append(("m13w", Design.load(WX_DESIGN_PATH)))
    return out


def run_predict(engine: Engine, now: datetime, days: int, refit: bool = False,
                models: list[tuple[str, Design]] | None = None,
                store=None) -> tuple[str, list[LivePick]]:
    # fmt: skip
    """Predict every scheduled game in the next `days` as of now with each live model version,
    and store it (change-only). A game whose final-report or prekick time has passed with no
    stored prediction at that point (the runner was down, or the data was rebuilt) also gets
    one as of that time, from what was known then. Returns the first model's (M12's) picks.
    `store`: an already loaded store (the hourly run shares it with m15)."""
    store = store if store is not None else loaded_store(engine)
    g = store.games
    upcoming = [i for i in g.index
                if now < g.at[i, "kickoff"] <= now + timedelta(days=days)
                and g.at[i, "status"] == "scheduled"]  # fmt: skip
    results = []
    for name, design in models or live_models():
        fits, _ = (live_model if refit else frozen_model)(store, design, now)
        version = version_of(design, fits, name)
        groups = [GROUPS[n] for n in design.groups]
        weights = market_weights(design)
        with engine.begin() as conn:
            for gid, point in missed_points(conn, store, version, upcoming, now):
                t = point_time(point, g.at[gid, "kickoff"])
                back = predict_games(store, design, fits, groups, [gid], t, weights)
                attach_moneylines(conn, back, t)
                store_picks(conn, back, version, t, model=name)
        picks = predict_games(store, design, fits, groups, upcoming, now, weights)
        with engine.begin() as conn:
            attach_moneylines(conn, picks, now)
            store_picks(conn, picks, version, now, model=name)
        results.append((version, picks))
    return results[0]


def missed_points(conn, store, version: str, game_ids, now: datetime) -> list[tuple[int, str]]:
    """(game, point) pairs whose time has passed, other than the current point, with no stored
    prediction of this model version at that point."""
    from sqlalchemy import select

    from betdata.db.schema import model_predictions as mp
    from betdata.nfl.predict import point_now

    if not game_ids:
        return []
    have = set(conn.execute(
        select(mp.c.game_id, mp.c.prediction_point)
        .where(mp.c.model_version == version, mp.c.game_id.in_([int(i) for i in game_ids]))
    ).all())  # fmt: skip
    out = []
    for gid in game_ids:
        kick = store.games.at[gid, "kickoff"]
        for p in POINTS:
            if now >= point_time(p, kick) and p != point_now(kick, now) and (gid, p) not in have:
                out.append((int(gid), p))
    return out


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
