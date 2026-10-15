"""Cross-source game matching: one row in `games` per real game, many source ids pointing at it."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Connection, and_, insert, select, update

from betdata.db.schema import game_source_ids, games, sports

# Sources disagree on kickoff times (TBD slots, flex scheduling); within this window with the
# same home/away teams it is the same game.
MATCH_WINDOW = timedelta(hours=36)


def sport_id(conn: Connection, key: str) -> int:
    sid = conn.execute(select(sports.c.sport_id).where(sports.c.key == key)).scalar()
    if sid is None:
        raise LookupError(f"sport {key!r} not seeded; run `betdata db init`")
    return sid


def season_for(sport: str, start: datetime) -> int:
    """Season is labelled by the calendar year it starts in (NFL 2026 runs into Feb 2027)."""
    if sport in ("nfl", "ncaaf"):
        return start.year if start.month >= 3 else start.year - 1
    return start.year if start.month >= 8 else start.year - 1  # nba, ncaab


def find_or_create_game(
    conn: Connection,
    *,
    source: str,
    source_game_id: str,
    sport: str,
    sport_id: int,
    home_team_id: int,
    away_team_id: int,
    start_time_utc: datetime,
    neutral_site: bool | None = None,
) -> int:
    game_id = conn.execute(
        select(game_source_ids.c.game_id).where(
            game_source_ids.c.source == source, game_source_ids.c.source_game_id == source_game_id
        )
    ).scalar()
    if game_id is not None:
        current = conn.execute(
            select(games.c.start_time_utc).where(games.c.game_id == game_id)
        ).scalar()
        if current != start_time_utc:
            conn.execute(
                update(games)
                .where(games.c.game_id == game_id)
                .values(start_time_utc=start_time_utc)
            )
        return game_id

    game_id = conn.execute(
        select(games.c.game_id).where(
            and_(
                games.c.sport_id == sport_id,
                games.c.home_team_id == home_team_id,
                games.c.away_team_id == away_team_id,
                games.c.start_time_utc.between(
                    start_time_utc - MATCH_WINDOW, start_time_utc + MATCH_WINDOW
                ),
            )
        )
    ).scalar()
    if game_id is None:
        game_id = conn.execute(
            insert(games).values(
                sport_id=sport_id,
                season=season_for(sport, start_time_utc),
                start_time_utc=start_time_utc,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
                neutral_site=neutral_site,
            )
        ).inserted_primary_key[0]
    conn.execute(
        insert(game_source_ids).values(
            source=source, source_game_id=source_game_id, game_id=game_id
        )
    )
    return game_id
