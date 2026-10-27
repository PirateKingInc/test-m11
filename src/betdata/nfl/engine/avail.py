"""M17 B5 step 9: M12's play probability of every candidate at each prediction point, for the
history games (cached per season under `derived/m17/`). Live, the engine reads the same M12
rules from the as-of store with the latest statuses (`nfl/engine/live.py`).

Rows: nflverse game id, point, team (abbreviation), gsis id, M12 group, role, bucket, p_play,
and `q` = 1 - p_play / p_healthy, the chance he misses relative to a player with no status
(the nflp15 convention: a player not on any report counts as available).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)
POINTS = ("final_report", "prekick")


def _store():
    from betdata.nfl.cli import _engine
    from betdata.nfl.props.history import load_store_all
    from betdata.nfl.workflow import fitted_params

    with _engine().connect() as conn:
        return fitted_params(load_store_all(conn))


def _ids() -> pd.Series:
    """main game id -> nflverse game id."""
    from sqlalchemy import select

    from betdata.db.schema import game_source_ids
    from betdata.nfl.cli import _engine

    with _engine().connect() as conn:
        rows = conn.execute(select(game_source_ids.c.game_id, game_source_ids.c.source_game_id)
                            .where(game_source_ids.c.source == "nflverse")).all()  # fmt: skip
    return pd.Series({g: s for g, s in rows})


def season_avail(store, season: int, ids: pd.Series) -> pd.DataFrame:
    from betdata.nfl.props.panel import availability, healthy_play, point_times

    g = store.games
    gids = [gid for gid in g.index if int(g.at[gid, "season"]) == season]
    slots = point_times(g, gids, POINTS)
    a = availability(store, slots)
    healthy = healthy_play(store.params)
    a["q"] = (1 - a.p_play / a.point.map(healthy)).clip(0, 1)
    a["nflverse_game_id"] = a.game_id.map(ids)
    return a


def load(seasons, data_dir: Path | None = None, rebuild: bool = False) -> pd.DataFrame:
    from betdata.nfl.engine.plays import cache_dir

    cd = cache_dir(data_dir)
    out, store, ids = [], None, None
    for s in seasons:
        path = cd / f"avail_{s}.parquet"
        if path.exists() and not rebuild:
            out.append(pd.read_parquet(path))
            continue
        if store is None:
            store, ids = _store(), _ids()
        log.info("m17 availability %s", s)
        a = season_avail(store, s, ids)
        cd.mkdir(parents=True, exist_ok=True)
        a.to_parquet(path)
        out.append(a)
    return pd.concat(out, ignore_index=True)
