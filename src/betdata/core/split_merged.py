"""Split games the old matcher merged (issue #156): one `games` row holding two events of the
same source, e.g. both games of an NBA two-game series 24 hours apart.

For each such game, the mappings whose source event starts near the game's own kickoff stay;
the others are detached and matched again from `source_events` with the fixed matcher, which
gives them their own game. Rows built from the mixed game are deleted so a reload rebuilds them
right: its results from the sources that had two events, and its NBA box scores and stints.

Odds and prop-line history is never changed by default: a game whose detached events have
stored odds or prop lines is reported and skipped. With `relink_odds=True` those rows are
pointed at the new game (only the `game_id` link column changes, as `betdata relink` does;
prices, lines and times stay as stored). Predictions, grades and injury-report rows stay on the
kept game and are counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import Connection, delete, func, insert, select, true, update

from betdata.core.matching import _utc
from betdata.core.models import ParsedEvent
from betdata.db.schema import (
    game_source_ids,
    games,
    model_predictions,
    nba_bet_grades,
    nba_injury_reports,
    nba_player_games,
    nba_stints,
    nfl_bet_grades,
    odds_snapshots,
    prop_grades,
    prop_lines,
    prop_predictions,
    results,
    source_events,
    sports,
)

SAME_GAME = timedelta(hours=6)  # mappings this close to the kept kickoff are the same game
KEPT_TABLES = (
    model_predictions,
    nba_bet_grades,
    nfl_bet_grades,
    prop_predictions,
    prop_grades,
    nba_injury_reports,
)


@dataclass
class SplitResult:
    game_id: int
    detached: list[tuple[str, str]]
    new_games: list[int] = field(default_factory=list)
    deleted: dict[str, int] = field(default_factory=dict)
    history_rows: int = 0
    relinked: int = 0
    left_on_kept: int = 0
    skipped: str | None = None


def merged_games(conn: Connection) -> list[int]:
    """Games holding two or more event ids of one source."""
    q = (select(game_source_ids.c.game_id).having(func.count() > 1)
         .group_by(game_source_ids.c.game_id, game_source_ids.c.source))  # fmt: skip
    return sorted(set(conn.execute(q).scalars()))


def _detached(conn: Connection, game) -> list:
    rows = conn.execute(
        select(game_source_ids.c.source, game_source_ids.c.source_game_id,
               game_source_ids.c.home_away_swapped, source_events.c.start_time_utc,
               source_events.c.home_name, source_events.c.away_name)
        .outerjoin(source_events, (source_events.c.source == game_source_ids.c.source)
                   & (source_events.c.source_game_id == game_source_ids.c.source_game_id))
        .where(game_source_ids.c.game_id == game.game_id)
    ).all()  # fmt: skip
    timed = {r.source_game_id: _utc(r.start_time_utc) for r in rows if r.start_time_utc}
    # The kept event: the timed mapping closest to the game's kickoff.
    anchor = min(timed.values(), key=lambda t: abs(t - _utc(game.start_time_utc)), default=None)
    return [r for r in rows if anchor and r.source_game_id in timed
            and abs(timed[r.source_game_id] - anchor) > SAME_GAME]  # fmt: skip


def _unmatched(conn: Connection, r) -> bool:
    """True when this detached mapping found no game when matched again."""
    return conn.execute(select(game_source_ids.c.game_id).where(
        game_source_ids.c.source == r.source,
        game_source_ids.c.source_game_id == r.source_game_id)).first() is None  # fmt: skip


def split_game(conn: Connection, game_id: int, *, relink_odds: bool = False) -> SplitResult:
    from betdata.core.odds_loader import resolve_events  # avoid import cycle

    game = conn.execute(select(games, sports.c.key.label("sport")).join(sports).where(
        games.c.game_id == game_id)).one()  # fmt: skip
    move = _detached(conn, game)
    out = SplitResult(game_id, [(r.source, r.source_game_id) for r in move])
    if not move:
        out.skipped = "no mapping far enough from the kickoff to split"
        return out
    keys = {(r.source, r.source_game_id) for r in move}
    ids = {r.source_game_id for r in move}
    odds = [r.snapshot_id for r in conn.execute(
        select(odds_snapshots.c.snapshot_id, odds_snapshots.c.source,
               odds_snapshots.c.source_game_id).where(odds_snapshots.c.game_id == game_id))
        if (r.source, r.source_game_id) in keys]  # fmt: skip
    props = list(conn.execute(select(prop_lines.c.prop_line_id).where(
        prop_lines.c.game_id == game_id, prop_lines.c.source_game_id.in_(ids))).scalars())
    out.history_rows = len(odds) + len(props)
    if out.history_rows and not relink_odds:
        out.skipped = f"{out.history_rows} odds/prop rows on the detached events (--relink-odds)"
        return out

    conn.execute(delete(game_source_ids).where(
        game_source_ids.c.game_id == game_id, game_source_ids.c.source.in_({s for s, _ in keys}),
        game_source_ids.c.source_game_id.in_(ids)))  # fmt: skip
    new_of: dict[str, int] = {}
    # The game's schedule owner first, so it creates the new game; others then match it.
    for r in sorted(move, key=lambda r: (r.source != game.schedule_source, r.source)):
        if r.home_name is None:
            continue
        ev = ParsedEvent(r.source_game_id, r.start_time_utc, r.home_name, r.away_name,
                         season=game.season, season_type=game.season_type)  # fmt: skip
        m = resolve_events(conn, source=r.source, sport=game.sport, events=[ev], record=False)
        if m[r.source_game_id] is not None:
            new_of.setdefault(r.source_game_id, m[r.source_game_id].game_id)
    for r in move:
        if r.home_name is not None and not _unmatched(conn, r):
            continue
        # An id with no stored event (e.g. "espn") follows a sibling with the same id; one that
        # could not be matched again goes back where it was.
        sibling = conn.execute(select(game_source_ids.c.home_away_swapped).where(
            game_source_ids.c.source_game_id == r.source_game_id,
            game_source_ids.c.game_id == new_of.get(r.source_game_id))).scalar()
        conn.execute(insert(game_source_ids).values(
            source=r.source, source_game_id=r.source_game_id,
            game_id=new_of.get(r.source_game_id, game_id),
            home_away_swapped=r.home_away_swapped if sibling is None else sibling))  # fmt: skip
    out.new_games = sorted(set(new_of.values()) - {game_id})

    for table, where in ((results, results.c.source.in_({s for s, _ in keys})),
                         (nba_player_games, true()), (nba_stints, true())):  # fmt: skip
        n = conn.execute(delete(table).where(table.c.game_id == game_id, where)).rowcount
        out.deleted[table.name] = n
    for table, pk, rows in ((odds_snapshots, "snapshot_id", odds),
                            (prop_lines, "prop_line_id", props)):  # fmt: skip
        for i in range(0, len(rows), 500):
            for sgid, new in new_of.items():
                out.relinked += conn.execute(update(table).where(
                    table.c[pk].in_(rows[i:i + 500]), table.c.source_game_id == sgid)
                    .values(game_id=new)).rowcount  # fmt: skip
    out.left_on_kept = sum(conn.execute(select(func.count()).select_from(t).where(
        t.c.game_id == game_id)).scalar() for t in KEPT_TABLES)  # fmt: skip
    return out
