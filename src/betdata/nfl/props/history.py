"""Features and actuals of every played QB/RB/WR/TE game 2013-2025 at both M12 points, built
from the database and the nflverse files, cached under data/derived."""

from __future__ import annotations

import logging

import pandas as pd

from betdata.nfl.pipeline import TEST, TRAIN, VALIDATE

log = logging.getLogger(__name__)
SEASONS = (*TRAIN, *VALIDATE, *TEST)
ACTUALS = ("pass_yds", "rush_yds", "rec_yds", "rec", "td", "tgt", "car", "att")


def cache_path():
    from betdata.nfl.workflow import data_dir

    return data_dir() / "derived" / "nfl_prop_features.pkl"


def avail_cached(store, slots: pd.DataFrame, rebuild: bool = False) -> pd.DataFrame:
    from betdata.nfl.props.panel import availability
    from betdata.nfl.workflow import data_dir

    path = data_dir() / "derived" / f"nfl_prop_avail_{len(slots)}.parquet"
    if path.exists() and not rebuild:
        return pd.read_parquet(path)
    a = availability(store, slots)
    path.parent.mkdir(parents=True, exist_ok=True)
    a.to_parquet(path)
    return a


def history_features(rebuild: bool = False) -> pd.DataFrame:
    path = cache_path()
    if path.exists() and not rebuild:
        return pd.read_pickle(path)
    from betdata.nfl.cli import _engine
    from betdata.nfl.groups import GROUPS
    from betdata.nfl.pipeline import outcomes
    from betdata.nfl.props.panel import Panel, healthy_play, point_times, teammate_context
    from betdata.nfl.props.side import side_history
    from betdata.nfl.selection import Design
    from betdata.nfl.weather import WX_DESIGN_PATH
    from betdata.nfl.workflow import (
        data_dir,
        design_rows,
        feature_rows,
        fitted_params,
        model_games,
    )

    engine = _engine()
    with engine.connect() as conn:
        store = fitted_params(load_store_all(conn))
        panel = Panel.load(conn, store, range(min(SEASONS) - 1, max(SEASONS) + 1), data_dir())
    design = Design.load(WX_DESIGN_PATH)
    # the weather group is rebuilt first (its data grows with backfills; as `nfl weather`)
    feature_rows(store, GROUPS["wx"], model_games(store, SEASONS), refresh=True)
    rows = design_rows(store, design, SEASONS)
    side = side_history(rows, outcomes(store), design)
    slots = point_times(store.games, model_games(store, SEASONS))
    log.info("availability for %d slots", len(slots))
    mates = teammate_context(avail_cached(store, slots), panel.states, slots,
                             healthy_play(store.params))
    tg = panel.played_targets(SEASONS)
    f = panel.features(tg, side, mates)
    y = panel.box.set_index(["game_id", "player_id"])[list(ACTUALS)]
    y = y[~y.index.duplicated()]
    f = f.join(y.rename(columns=lambda c: f"y_{c}"), on=["game_id", "player_id"])
    t = panel.team.set_index(["game_id", "team_id"])[["t_att", "t_tgt", "t_car"]]
    f = f.join(t.rename(columns=lambda c: "yt_" + c[2:]), on=["game_id", "team_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    f.to_pickle(path)
    return f


def load_store_all(conn):
    from betdata.nfl.data import load_store

    return load_store(conn)
