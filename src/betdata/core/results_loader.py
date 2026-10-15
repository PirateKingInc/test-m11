"""Game scores. One row per (game, source); updated in place as a game goes final or is corrected.

Unlike odds, results are facts rather than a time series, so they are upserted.
"""

from __future__ import annotations

from sqlalchemy import Connection, insert, select, update

from betdata.core.matching import GameMatch
from betdata.core.models import ParsedEvent
from betdata.db.schema import games, results, utcnow


def load_results(
    conn: Connection,
    *,
    source: str,
    events: list[ParsedEvent],
    matches: dict[str, GameMatch | None],
) -> int:
    """Returns the number of result rows inserted or changed."""
    changed = 0
    for ev in events:
        match = matches.get(ev.source_game_id)
        if match is None or ev.home_score is None or ev.away_score is None:
            continue
        home, away = (
            (ev.away_score, ev.home_score) if match.swapped else (ev.home_score, ev.away_score)
        )
        values = dict(home_score=home, away_score=away, final=ev.final)
        current = conn.execute(
            select(
                results.c.result_id, results.c.home_score, results.c.away_score, results.c.final
            ).where(results.c.game_id == match.game_id, results.c.source == source)
        ).first()
        if current is None:
            conn.execute(
                insert(results).values(
                    game_id=match.game_id, source=source, updated_at=utcnow(), **values
                )
            )
            changed += 1
        elif (current.home_score, current.away_score, current.final) != (home, away, ev.final):
            conn.execute(
                update(results)
                .where(results.c.result_id == current.result_id)
                .values(updated_at=utcnow(), **values)
            )
            changed += 1
        if ev.final:
            conn.execute(
                update(games)
                .where(games.c.game_id == match.game_id, games.c.status != "final")
                .values(status="final")
            )
    return changed
