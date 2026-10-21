"""NFL prop grading (`betdata grade nfl --props`, and the NFL plugin's hourly grade).

Actuals come from nflverse's weekly player stats file for the season (`stats_player`, refreshed
by the daily `nfl-player-stats` job of the nflverse_extra source; nflverse publishes a week's
stats overnight, so a game is graded the morning after, not within the hour). A predicted
player (ESPN id, mapped to his gsis id as in live.py) with a stat line is graded on it; one
without a stat line in a game whose stats are in is graded 0 if M12's snap counts show offensive
snaps for him, `void` if the game's snap counts are in and he has none (did not play), and left
pending otherwise.
"""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
from sqlalchemy import Connection, select

from betdata.config import PROJECT_ROOT
from betdata.db.schema import (
    game_source_ids,
    games,
    player_game_snaps,
    prop_predictions,
    roster_weeks,
)
from betdata.props.grade import VOID, GradeRun, grade_props

MODEL = "nflp15"
DESIGN_PATH = PROJECT_ROOT / "config" / "nfl_props.json"
STAT = {"pass_yds": ("passing_yards",), "rush_yds": ("rushing_yards",),
        "rec_yds": ("receiving_yards",), "receptions": ("receptions",),
        "anytime_td": ("rushing_tds", "receiving_tds")}  # fmt: skip


def thresholds() -> dict[str, float]:
    """Validated edge thresholds: none for NFL (no betting backtest was possible)."""
    if not DESIGN_PATH.exists():
        return {}
    d = json.loads(DESIGN_PATH.read_text())
    return {m: float(t) for m, t in (d.get("thresholds") or {}).items() if t is not None}


def actuals(conn: Connection, keys: list[tuple[int, str, str]], data_dir=None) -> dict:
    from betdata.nfl.nflverse_files import load_player_stats
    from betdata.nfl.props.ids import espn_to_gsis
    from betdata.nfl.workflow import data_dir as default_dir

    if not keys:
        return {}
    data_dir = data_dir or default_dir()
    gids = sorted({k[0] for k in keys})
    info = pd.DataFrame(conn.execute(
        select(games.c.game_id, games.c.season, game_source_ids.c.source_game_id)
        .join(game_source_ids, game_source_ids.c.game_id == games.c.game_id)
        .where(games.c.game_id.in_(gids), game_source_ids.c.source == "nflverse")).all(),
        columns=["game_id", "season", "nid"]).set_index("game_id")  # fmt: skip
    if info.empty:
        return {}
    seasons = sorted(info.season.unique())
    cols = ["player_id", "player_display_name", "game_id", *{c for v in STAT.values() for c in v}]
    stats = load_player_stats(seasons, columns=cols, data_dir=data_dir)
    names = pd.DataFrame(conn.execute(
        select(prop_predictions.c.game_id, prop_predictions.c.player_id,
               prop_predictions.c.player_name).where(prop_predictions.c.game_id.in_(gids))
        .distinct()).all(), columns=["game_id", "player_id", "player_name"])  # fmt: skip
    names = names.drop_duplicates(["game_id", "player_id"])
    names["gsis"] = espn_to_gsis(names, data_dir, (*seasons, max(seasons) + 1),
                                 stats.rename(columns={"player_display_name": "name"}))
    gsis = {(r.game_id, r.player_id): r.gsis for r in names.itertuples()}
    line = stats.set_index(["game_id", "player_id"])
    have_stats = set(stats.game_id)
    snapped = _snaps(conn, gids)
    out = {}
    for gid, pid, market in keys:
        nid = info.nid.get(gid)
        g = gsis.get((gid, pid))
        if nid not in have_stats or g is None:
            continue  # stats not in yet (or player not mapped): stays pending
        if (nid, g) in line.index:
            r = line.loc[(nid, g)]
            r = r.iloc[0] if isinstance(r, pd.DataFrame) else r
            out[(gid, pid, market)] = float(sum(float(r[c] or 0) for c in STAT[market]))
        elif gid in snapped:
            out[(gid, pid, market)] = 0.0 if g in snapped[gid] else VOID
    return out


def _snaps(conn: Connection, gids) -> dict[int, set[str]]:
    """game -> gsis ids with offensive snaps (games whose snap counts are in)."""
    s = player_game_snaps
    rows = conn.execute(select(s.c.game_id, s.c.season, s.c.pfr_player_id, s.c.offense_snaps)
                        .where(s.c.game_id.in_(gids))).all()  # fmt: skip
    if not rows:
        return {}
    seasons = {r.season for r in rows}
    pfr = dict(conn.execute(select(roster_weeks.c.pfr_id, roster_weeks.c.gsis_id)
                            .where(roster_weeks.c.season.in_(seasons),
                                   roster_weeks.c.pfr_id.is_not(None),
                                   roster_weeks.c.gsis_id.is_not(None))).all())  # fmt: skip
    out: dict[int, set[str]] = {}
    for r in rows:
        ids = out.setdefault(r.game_id, set())
        if (r.offense_snaps or 0) > 0 and r.pfr_player_id in pfr:
            ids.add(pfr[r.pfr_player_id])
    return out


def grade_nfl_props(conn: Connection, now: datetime) -> GradeRun:
    return grade_props(conn, now, (MODEL,), actuals, thresholds())
