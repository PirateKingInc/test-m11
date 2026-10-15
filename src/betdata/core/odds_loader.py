"""Writes parsed odds as append-only snapshots.

A row is inserted only when a selection's line, price or status differs from its previous
snapshot; every poll itself is recorded in `ingest_runs`, so "unchanged as of time T" is still
answerable. A selection that disappears from a requested market of an event still in the
response gets a `removed` row carrying its last known line/price, instead of silently vanishing.

Loading the same raw response twice inserts nothing the second time.

Outcomes are stored in the source's own home/away orientation; see core/matching.py and the
`v_odds_snapshots` view for the game-oriented version.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import Connection, insert, select

from betdata.core.matching import GameMatch, find_or_create_game, sport_id
from betdata.core.models import ParsedOdds
from betdata.core.normalize.catalog import BookResolver, MarketResolver
from betdata.core.normalize.teams import TeamResolver
from betdata.db.schema import odds_snapshots

log = logging.getLogger(__name__)
_CHUNK = 500  # stay under SQLite's bound-parameter limit


@dataclass
class LoadStats:
    events: int = 0
    events_unmatched: int = 0
    prices_seen: int = 0
    inserted: int = 0
    removed: int = 0
    matches: dict[str, GameMatch | None] = field(default_factory=dict)


def _latest_snapshots(conn, source, event_ids, at):
    latest = {}
    ids = sorted(event_ids)
    for i in range(0, len(ids), _CHUNK):
        rows = conn.execute(
            select(
                odds_snapshots.c.source_game_id,
                odds_snapshots.c.book_id,
                odds_snapshots.c.market_id,
                odds_snapshots.c.outcome,
                odds_snapshots.c.line,
                odds_snapshots.c.price_american,
                odds_snapshots.c.price_decimal,
                odds_snapshots.c.price_status,
                odds_snapshots.c.fetched_at,
                odds_snapshots.c.game_id,
                odds_snapshots.c.snapshot_kind,
            )
            .where(
                odds_snapshots.c.source == source,
                odds_snapshots.c.source_game_id.in_(ids[i : i + _CHUNK]),
                odds_snapshots.c.fetched_at <= at,
            )
            .order_by(odds_snapshots.c.fetched_at, odds_snapshots.c.snapshot_id)
        )
        for r in rows:
            latest[(r.source_game_id, r.book_id, r.market_id, r.outcome, r.snapshot_kind)] = r
    return latest


def resolve_events(
    conn: Connection,
    *,
    source: str,
    sport: str,
    events,
    authoritative: bool = False,
    record: bool = True,
) -> dict[str, GameMatch | None]:
    """Map each event to a game (creating games as needed). None = a team is unmatched.

    `record=True` stores the events in source_events and unmatched names in the review queue.
    """
    from betdata.core.teams_admin import record_events

    sid = sport_id(conn, sport)
    team_resolver = TeamResolver(conn, sid)
    team = team_resolver.resolve if record else team_resolver.lookup
    if record:
        record_events(conn, source=source, sport=sport, events=events)
    out: dict[str, GameMatch | None] = {}
    for ev in events:
        home = team(source, ev.home_name)
        away = team(source, ev.away_name)
        if home is None or away is None:
            out[ev.source_game_id] = None
            continue
        out[ev.source_game_id] = find_or_create_game(
            conn,
            source=source,
            source_game_id=ev.source_game_id,
            sport=sport,
            sport_id=sid,
            home_team_id=home,
            away_team_id=away,
            start_time_utc=ev.start_time_utc,
            neutral_site=ev.neutral_site,
            season=ev.season,
            season_type=ev.season_type,
            week=ev.week,
            authoritative=authoritative,
        )
    return out


def load_odds(
    conn: Connection, *, source: str, batch: ParsedOdds, run_id: int | None, raw_id: int | None
) -> LoadStats:
    stats = LoadStats(events=len(batch.events), prices_seen=len(batch.prices))
    book_resolver = BookResolver(conn, source)
    market_resolver = MarketResolver(conn)
    at = batch.fetched_at

    matches = resolve_events(
        conn,
        source=source,
        sport=batch.sport,
        events=batch.events,
        authoritative=batch.schedule_authority,
    )
    stats.matches = matches
    stats.events_unmatched = sum(1 for m in matches.values() if m is None)
    game_ids = {k: (m.game_id if m else None) for k, m in matches.items()}

    prior = _latest_snapshots(conn, source, game_ids.keys(), at)
    common = dict(source=source, fetched_at=at, run_id=run_id, raw_id=raw_id)
    rows, seen = [], set()

    for p in batch.prices:
        key = (
            p.source_game_id,
            book_resolver.resolve(p.book_key, p.book_title),
            market_resolver.resolve(p.market_key, p.period),
            p.outcome,
            p.snapshot_kind,
        )
        if key in seen:
            log.warning("duplicate selection in one response, keeping first: %s", key)
            continue
        seen.add(key)
        prev = prior.get(key)
        if prev is not None and (
            prev.fetched_at == at  # this response was already loaded
            or (prev.line, prev.price_american, prev.price_status)
            == (p.line, p.price_american, p.price_status)
        ):
            continue
        rows.append(
            dict(
                common,
                source_game_id=p.source_game_id,
                game_id=game_ids.get(p.source_game_id),
                book_id=key[1],
                market_id=key[2],
                outcome=p.outcome,
                snapshot_kind=p.snapshot_kind,
                line=p.line,
                price_american=p.price_american,
                price_decimal=p.price_decimal,
                price_status=p.price_status,
                book_last_update=p.book_last_update,
            )
        )
    stats.inserted = len(rows)

    market_ids = {market_resolver.resolve(m) for m in batch.requested_markets}
    book_ids = (
        None
        if batch.requested_books is None
        else {book_resolver.resolve(b) for b in batch.requested_books}
    )
    for key, prev in prior.items():
        event_id, book_id, market_id, outcome, kind = key
        if (
            kind != "poll"  # removal only applies to live polling, not source closing lines
            or key in seen
            or event_id not in game_ids
            or market_id not in market_ids
            or (book_ids is not None and book_id not in book_ids)
            or prev.price_status == "removed"
            or prev.fetched_at == at
        ):
            continue
        rows.append(
            dict(
                common,
                source_game_id=event_id,
                game_id=game_ids.get(event_id) or prev.game_id,
                book_id=book_id,
                market_id=market_id,
                outcome=outcome,
                snapshot_kind="poll",
                line=prev.line,
                price_american=prev.price_american,
                price_decimal=prev.price_decimal,
                price_status="removed",
                book_last_update=None,
            )
        )
        stats.removed += 1

    if rows:
        conn.execute(insert(odds_snapshots), rows)
    return stats
