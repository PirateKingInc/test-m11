"""Cross-source game matching: one row in `games` per real game, many source ids pointing at it.

Sources can disagree about which team is "home" at neutral sites (London, Super Bowl, bowls).
Odds rows keep the source's own orientation forever; `game_source_ids.home_away_swapped`
records how that source relates to the game's orientation, and the `v_odds_snapshots` view
applies it. An authoritative schedule source (e.g. nflverse) owns the game's schedule fields
and orientation; other sources may only update a game they created themselves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, and_, insert, or_, select, update

from betdata.db.schema import game_source_ids, games, sports

log = logging.getLogger(__name__)

# Sources disagree on kickoff times (TBD slots posted as midnight ET, flex scheduling, date-only
# feeds); within this window with the same two teams it can be the same game. Two real games
# between the same teams can also fall inside it (NBA two-game series, 24 h apart), so a match
# takes the closest kickoff, and never a game that already holds another event of the same
# source (issue #156).
MATCH_WINDOW = timedelta(hours=36)
# A claimed game this close is probably the same game re-listed under a new id: logged.
RELIST_WINDOW = timedelta(hours=3)


@dataclass(frozen=True)
class GameMatch:
    game_id: int
    swapped: bool  # source's home team is the game's away team
    created: bool


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


def _claimed(conn, game_ids, ids: dict[str, str]) -> set[int]:
    """Games among `game_ids` already holding a different event id of one of `ids`' sources."""
    if not game_ids or not ids:
        return set()
    clauses = [and_(game_source_ids.c.source == src, game_source_ids.c.source_game_id != sgid)
               for src, sgid in ids.items()]  # fmt: skip
    return set(conn.execute(
        select(game_source_ids.c.game_id).where(
            game_source_ids.c.game_id.in_(game_ids), or_(*clauses))
    ).scalars())  # fmt: skip


def _candidates(conn, sport_id, home, away, start, ids=None):
    """[(game_id, swapped)] with these teams (either orientation) within MATCH_WINDOW, closest
    kickoff first. With `ids` ({source: event id}), games already holding a different event id
    of one of those sources are left out."""
    rows = conn.execute(
        select(games.c.game_id, games.c.home_team_id, games.c.start_time_utc).where(
            games.c.sport_id == sport_id,
            or_(and_(games.c.home_team_id == home, games.c.away_team_id == away),
                and_(games.c.home_team_id == away, games.c.away_team_id == home)),
            games.c.start_time_utc.between(start - MATCH_WINDOW, start + MATCH_WINDOW),
        )
    ).all()  # fmt: skip
    # Same orientation wins a tie (the old matcher tried it first).
    rows.sort(key=lambda r: (abs(_utc(r.start_time_utc) - _utc(start)), r.home_team_id != home,
                             r.game_id))  # fmt: skip
    taken = _claimed(conn, [r.game_id for r in rows], ids or {})
    out = [(r.game_id, r.home_team_id != home) for r in rows if r.game_id not in taken]
    for r in rows:
        if r.game_id in taken and abs(_utc(r.start_time_utc) - _utc(start)) <= RELIST_WINDOW:
            log.warning("game %s at %s already holds another event of %s; not matched to %s",
                        r.game_id, r.start_time_utc, sorted(ids), ids)  # fmt: skip
    return out


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _flip_game(conn, game_id: int) -> None:
    """Swap the game's home/away and every source mapping's swapped flag (history untouched)."""
    g = conn.execute(
        select(games.c.home_team_id, games.c.away_team_id).where(games.c.game_id == game_id)
    ).one()
    conn.execute(
        update(games)
        .where(games.c.game_id == game_id)
        .values(home_team_id=g.away_team_id, away_team_id=g.home_team_id)
    )
    conn.execute(
        update(game_source_ids)
        .where(game_source_ids.c.game_id == game_id)
        .values(home_away_swapped=~game_source_ids.c.home_away_swapped)
    )


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
    season: int | None = None,
    season_type: str | None = None,
    week: int | None = None,
    status: str | None = None,
    venue_id: int | None = None,
    roof: str | None = None,
    authoritative: bool = False,
    other_ids: dict[str, str] | None = None,
) -> GameMatch:
    """`other_ids`: this event's ids in other sources (e.g. {"espn": "401..."}). A game that
    already holds a different event id of this source or of one of those is never matched."""
    schedule = dict(
        start_time_utc=start_time_utc, season=season or season_for(sport, start_time_utc)
    )
    for k, v in dict(
        season_type=season_type,
        week=week,
        neutral_site=neutral_site,
        status=status,
        venue_id=venue_id,
        roof=roof,
    ).items():
        if v is not None:
            schedule[k] = v

    known = conn.execute(
        select(game_source_ids.c.game_id, game_source_ids.c.home_away_swapped).where(
            game_source_ids.c.source == source, game_source_ids.c.source_game_id == source_game_id
        )
    ).first()
    if known is not None:
        game_id, swapped = known
        owner = conn.execute(
            select(games.c.schedule_source).where(games.c.game_id == game_id)
        ).scalar()
        if authoritative and swapped:
            _flip_game(conn, game_id)
            swapped = False
        if authoritative or owner in (None, source):
            values = dict(schedule, schedule_source=source) if authoritative else schedule
            conn.execute(update(games).where(games.c.game_id == game_id).values(**values))
        return GameMatch(game_id, swapped, created=False)

    created, swapped = False, False
    found = _candidates(conn, sport_id, home_team_id, away_team_id, start_time_utc,
                        {**(other_ids or {}), source: source_game_id})  # fmt: skip
    game_id, swapped = found[0] if found else (None, False)

    if game_id is None:
        game_id = conn.execute(
            insert(games).values(
                sport_id=sport_id,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
                schedule_source=source,
                **schedule,
            )
        ).inserted_primary_key[0]
        created = True
    elif authoritative:
        if swapped:
            _flip_game(conn, game_id)
            swapped = False
        conn.execute(
            update(games)
            .where(games.c.game_id == game_id)
            .values(**schedule, schedule_source=source)
        )

    conn.execute(
        insert(game_source_ids).values(
            source=source,
            source_game_id=source_game_id,
            game_id=game_id,
            home_away_swapped=swapped,
        )
    )
    return GameMatch(game_id, swapped, created)
