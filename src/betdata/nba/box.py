"""Player box scores into `nba_player_games`, shared by the sportsdataverse season files and
ESPN's per-game summaries. Games are found by their ESPN id (both sources use it)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import Connection, delete, insert, select

from betdata.core.matching import sport_id
from betdata.core.normalize.teams import TeamResolver
from betdata.db.schema import game_source_ids, nba_player_games

log = logging.getLogger(__name__)
RESULT_DELAY = timedelta(hours=3)  # a box score is known this long after tip (history)
FIELDS = ("player_id", "player_name", "starter", "did_not_play", "reason", "minutes", "points",
          "plus_minus")  # fmt: skip


def espn_games(conn: Connection, espn_ids) -> dict[str, int]:
    """ESPN game id -> our game id, for games already loaded from a schedule."""
    ids = sorted({str(i) for i in espn_ids})
    out: dict[str, int] = {}
    for i in range(0, len(ids), 500):
        out.update(conn.execute(
            select(game_source_ids.c.source_game_id, game_source_ids.c.game_id)
            .where(game_source_ids.c.source == "espn",
                   game_source_ids.c.source_game_id.in_(ids[i:i + 500]))).all())  # fmt: skip
    return out


def load_box_rows(conn: Connection, rows: list[dict], *, source: str,
                  fetched_at: datetime, live: bool = False) -> tuple[int, int]:  # fmt: skip
    """rows: one per player with espn_game_id, team (name or abbreviation), tip (UTC) and FIELDS.
    Replaces each game's stored rows. Returns (rows inserted, games without a match)."""
    games = espn_games(conn, (r["espn_game_id"] for r in rows))
    teams = TeamResolver(conn, sport_id(conn, "nba"))
    by_game: dict[int, list[dict]] = {}
    missing: set[str] = set()
    for r in rows:
        gid = games.get(str(r["espn_game_id"]))
        team = teams.resolve(source, r["team"]) if gid else None
        if gid is None or team is None:
            missing.add(str(r["espn_game_id"]))
            continue
        known = fetched_at if live else r["tip"] + RESULT_DELAY
        by_game.setdefault(gid, []).append(dict(
            game_id=gid, team_id=team, source=source, known_at=known, fetched_at=fetched_at,
            **{k: r.get(k) for k in FIELDS}))  # fmt: skip
    n = 0
    for gid, items in by_game.items():
        seen, unique = set(), []
        for it in items:
            if it["player_id"] not in seen:
                seen.add(it["player_id"])
                unique.append(it)
        conn.execute(delete(nba_player_games).where(nba_player_games.c.game_id == gid))
        conn.execute(insert(nba_player_games), unique)
        n += len(unique)
    if missing:
        log.info("%s: %d games with box scores but no matching game", source, len(missing))
    return n, len(missing)
