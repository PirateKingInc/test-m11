"""Team registry maintenance: importing teams, adding aliases, relinking stored events.

Aliases added here are written to the database and appended to seeds/team_aliases.csv, so
a rebuilt database (`betdata db init`) gets them back without anyone re-reviewing names.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, insert, select, update

from betdata.core.matching import sport_id as get_sport_id
from betdata.core.models import ParsedEvent
from betdata.core.normalize.teams import TeamResolver
from betdata.db import seed as _seed
from betdata.db.schema import (
    game_source_ids,
    odds_snapshots,
    source_events,
    team_aliases,
    team_source_ids,
    teams,
    unmatched_entities,
    utcnow,
)

log = logging.getLogger(__name__)
ALIAS_FIELDS = ["sport", "source", "alias", "canonical_name"]


class AliasConflict(ValueError):
    pass


def upsert_team(
    conn: Connection,
    *,
    sport_id: int,
    canonical_name: str,
    abbrev: str | None = None,
    conference: str | None = None,
    division: str | None = None,
    division_level: str | None = None,
) -> int:
    """Insert, or refresh conference/division on an existing team (they change with realignment)."""
    values = dict(
        abbrev=abbrev, conference=conference, division=division, division_level=division_level
    )
    team_id = conn.execute(
        select(teams.c.team_id).where(
            teams.c.sport_id == sport_id, teams.c.canonical_name == canonical_name
        )
    ).scalar()
    if team_id is None:
        return conn.execute(
            insert(teams).values(
                sport_id=sport_id, canonical_name=canonical_name, active=True, **values
            )
        ).inserted_primary_key[0]
    conn.execute(
        update(teams)
        .where(teams.c.team_id == team_id)
        .values(**{k: v for k, v in values.items() if v is not None})
    )
    return team_id


def link_source_team(conn, *, source: str, sport_id: int, source_team_id: str, team_id: int):
    existing = conn.execute(
        select(team_source_ids.c.team_id).where(
            team_source_ids.c.source == source,
            team_source_ids.c.sport_id == sport_id,
            team_source_ids.c.source_team_id == source_team_id,
        )
    ).scalar()
    if existing is None:
        conn.execute(
            insert(team_source_ids).values(
                source=source, sport_id=sport_id, source_team_id=source_team_id, team_id=team_id
            )
        )


def add_alias(
    conn: Connection,
    *,
    sport: str,
    source: str,
    alias: str,
    team_id: int,
    persist: bool = True,
    seeds_dir: Path | None = None,
) -> bool:
    """Map `alias` (as written by `source`, or "*" for any) to a team.

    Returns False if it already existed. Raises AliasConflict if it points at another team.
    """
    sid = get_sport_id(conn, sport)
    current = conn.execute(
        select(team_aliases.c.team_id).where(
            team_aliases.c.source == source,
            team_aliases.c.sport_id == sid,
            team_aliases.c.alias == alias,
        )
    ).scalar()
    if current == team_id:
        return False
    if current is not None:
        raise AliasConflict(f"{alias!r} ({source}, {sport}) already maps to team {current}")
    conn.execute(
        insert(team_aliases).values(source=source, sport_id=sid, alias=alias, team_id=team_id)
    )
    where = [
        unmatched_entities.c.sport_id == sid,
        unmatched_entities.c.kind == "team",
        unmatched_entities.c.raw_value == alias,
    ]
    if source != "*":
        where.append(unmatched_entities.c.source == source)
    conn.execute(update(unmatched_entities).where(*where).values(resolved=True))
    if persist:
        canonical = conn.execute(
            select(teams.c.canonical_name).where(teams.c.team_id == team_id)
        ).scalar()
        _append_alias_csv(
            (seeds_dir or _seed.SEEDS) / "team_aliases.csv", sport, source, alias, canonical
        )
    return True


def _append_alias_csv(path: Path, sport, source, alias, canonical) -> None:
    new_file = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ALIAS_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(dict(sport=sport, source=source, alias=alias, canonical_name=canonical))


def find_team(conn: Connection, sport: str, name: str) -> int | None:
    """Exact canonical name, else any alias/normalized match (for CLI input)."""
    sid = get_sport_id(conn, sport)
    return TeamResolver(conn, sid).lookup("*", name)


def record_events(conn: Connection, *, source: str, sport: str, events: list[ParsedEvent]) -> None:
    if not events:
        return
    sid = get_sport_id(conn, sport)
    now = utcnow()
    ids = [e.source_game_id for e in events]
    known: set[str] = set()
    for i in range(0, len(ids), 500):  # stay under SQLite's bound-parameter limit
        known.update(
            conn.execute(
                select(source_events.c.source_game_id).where(
                    source_events.c.source == source,
                    source_events.c.source_game_id.in_(ids[i : i + 500]),
                )
            ).scalars()
        )
    for ev in events:
        values = dict(home_name=ev.home_name, away_name=ev.away_name,
                      start_time_utc=ev.start_time_utc, last_seen=now)  # fmt: skip
        if ev.source_game_id in known:
            conn.execute(
                update(source_events)
                .where(
                    source_events.c.source == source,
                    source_events.c.source_game_id == ev.source_game_id,
                )
                .values(**values)
            )
        else:
            conn.execute(
                insert(source_events).values(
                    source=source,
                    source_game_id=ev.source_game_id,
                    sport_id=sid,
                    first_seen=now,
                    **values,
                )  # fmt: skip
            )


@dataclass
class RelinkStats:
    events_checked: int = 0
    events_linked: int = 0
    snapshots_linked: int = 0


def relink(conn: Connection, *, source: str | None = None) -> RelinkStats:
    """Give games to stored events that had an unmapped team, and point their odds at them.

    Only fills odds_snapshots.game_id where it is null; no odds values are changed.
    """
    from betdata.core.odds_loader import resolve_events  # avoid import cycle
    from betdata.db.schema import sports

    stats = RelinkStats()
    q = (
        select(source_events, sports.c.key.label("sport"))
        .join(sports, sports.c.sport_id == source_events.c.sport_id)
        .outerjoin(
            game_source_ids,
            (game_source_ids.c.source == source_events.c.source)
            & (game_source_ids.c.source_game_id == source_events.c.source_game_id),
        )
        .where(game_source_ids.c.game_id.is_(None))
    )
    if source:
        q = q.where(source_events.c.source == source)
    pending: dict[tuple[str, str], list[ParsedEvent]] = {}
    for r in conn.execute(q):
        stats.events_checked += 1
        pending.setdefault((r.source, r.sport), []).append(
            ParsedEvent(r.source_game_id, r.start_time_utc, r.home_name, r.away_name)
        )
    for (src, sport), events in pending.items():
        matches = resolve_events(conn, source=src, sport=sport, events=events, record=False)
        for event_id, match in matches.items():
            if match is None:
                continue
            stats.events_linked += 1
            stats.snapshots_linked += conn.execute(
                update(odds_snapshots)
                .where(
                    odds_snapshots.c.source == src,
                    odds_snapshots.c.source_game_id == event_id,
                    odds_snapshots.c.game_id.is_(None),
                )
                .values(game_id=match.game_id)
            ).rowcount
    return stats
