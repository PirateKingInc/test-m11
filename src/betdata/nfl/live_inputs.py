"""Refresh the compact live inputs of the M15 NFL versions under `<data_dir>/derived` from the
files on disk: m15's per-season team-game and kicker tables and depth charts
(nfl/m15cache.py), and the prop panel's per-season player-game rows and ESPN->gsis id map
(nfl/props). Run by the daily 7am job `nfl-inputs15` after the day's downloads, and by the
one-off `nfl-m15-bootstrap` workflow after it downloads the earlier seasons. No network, no
refit: the expected-rate fits come from m15's frozen fit file."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import Connection

from betdata.core.matching import season_for

log = logging.getLogger(__name__)


def refresh(conn: Connection, now: datetime, data_dir: Path,
            m15_seasons: int | None = None) -> dict[str, int]:  # fmt: skip
    """Rows now cached per part ({} parts that had nothing to read are logged and skipped)."""
    from betdata.nfl.live15 import LIVE_SEASONS, load15

    season = season_for("nfl", now)
    out: dict[str, int] = {}
    got = load15()
    if got is not None:
        from betdata.nfl.data import load_games
        from betdata.nfl.m15cache import cached_depth, cached_tables
        from betdata.nfl.m15data import Expected, _kickoffs, game_map

        exp = Expected.from_dict(got[3])
        first = season - (LIVE_SEASONS if m15_seasons is None else m15_seasons)
        try:
            tg, k = cached_tables(data_dir, range(first, season + 1), exp)
            out["m15_team_games"], out["m15_kicks"] = len(tg), len(k)
        except ValueError as e:
            log.warning("%s", e)
        kick = _kickoffs(game_map(conn), load_games(conn))
        out["m15_depth_rows"] = len(cached_depth(data_dir, season, kick))
    out.update(refresh_engine(season, data_dir))
    from betdata.nfl.props.grade import DESIGN_PATH

    if DESIGN_PATH.exists():
        from betdata.nfl.data import load_store
        from betdata.nfl.props.data import load_box_cached
        from betdata.nfl.props.ids import depth_ids

        seasons = (season - 1, season)
        store = load_store(conn, first_season=season - 1)
        box, _ = load_box_cached(conn, store.games, store.frame("snaps").df, seasons, data_dir)
        out["props_player_games"] = len(box)
        out["props_espn_ids"] = len(depth_ids(data_dir, seasons))
    return out


def refresh_engine(season: int, data_dir: Path) -> dict[str, int]:
    """M17 engine tables (drives, team-games, player-games) for the seasons it reads, rebuilt
    when that season's play-by-play is newer than the cache (no play-by-play: the cache is
    kept as it is)."""
    from betdata.nfl.engine.live import LIVE_SEASONS
    from betdata.nfl.engine.plays import season_tables

    out: dict[str, int] = {}
    for s in range(season - LIVE_SEASONS, season + 1):
        try:
            out[f"m17_team_games_{s}"] = len(season_tables(s, data_dir)["team_games"])
        except (FileNotFoundError, ValueError, KeyError) as e:
            log.warning("m17 tables %s: %s", s, e)
    return out


def sizes(data_dir: Path) -> dict[str, int]:
    """Bytes of the cached live inputs, by file name."""
    root = Path(data_dir) / "derived"
    return {p.name: p.stat().st_size for pat in ("nfl_m15_*", "nfl_prop_*", "nfl_espn_gsis.json")
            for p in sorted(root.glob(pat))}  # fmt: skip
