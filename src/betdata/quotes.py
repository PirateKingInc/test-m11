"""The market as it stood at an instant, for any sport: the latest polled spread, total and
moneyline per game, each with the book, the source and when it was fetched.

Snapshots are stored change-only, so `fetched_at` is when this number was first seen; the
caller knows when it last checked (the run time). Never a source's own closing line.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, column, select, table

from betdata.db.schema import UTCDateTime, books, markets

v_odds = table(
    "v_odds_snapshots",
    column("game_id"), column("market_id"), column("book_id"), column("game_outcome"),
    column("line"), column("price_american"), column("fetched_at", UTCDateTime),
    column("snapshot_kind"),
    column("source"), column("price_status"),
)  # fmt: skip

# (market key, outcome) -> name in the result
WANTED = {
    ("spreads", "home"): "spread_home",
    ("totals", "over"): "total",
    ("h2h", "home"): "ml_home",
    ("h2h", "away"): "ml_away",
}


@dataclass(frozen=True)
class Quote:
    line: float | None  # spread (home side) or total; None for a moneyline
    price: int | None  # American odds
    book: str
    source: str
    fetched_at: datetime


def current_quotes(conn: Connection, game_ids: Iterable[int],
                   now: datetime) -> dict[int, dict[str, Quote]]:  # fmt: skip
    """game_id -> {"spread_home", "total", "ml_home", "ml_away"} -> latest Quote fetched at or
    before `now`. The two moneyline sides come from the same source and book (the one with the
    latest home price)."""
    ids = list(game_ids)
    if not ids:
        return {}
    rows = conn.execute(
        select(v_odds.c.game_id, markets.c.key, v_odds.c.game_outcome, v_odds.c.line,
               v_odds.c.price_american, v_odds.c.source, v_odds.c.fetched_at,
               books.c.key.label("book"))
        .join(markets, markets.c.market_id == v_odds.c.market_id)
        .join(books, books.c.book_id == v_odds.c.book_id)
        .where(v_odds.c.game_id.in_(ids), v_odds.c.snapshot_kind == "poll",
               v_odds.c.price_status == "active", v_odds.c.fetched_at <= now,
               markets.c.period == "game", markets.c.key.in_(("spreads", "totals", "h2h")))
        .order_by(v_odds.c.fetched_at, books.c.key)
    ).all()  # fmt: skip
    out: dict[int, dict[str, Quote]] = {}
    away: dict[tuple[int, str, str], Quote] = {}  # (game, source, book) -> latest away price
    for r in rows:
        name = WANTED.get((r.key, r.game_outcome))
        if name is None or (name in ("spread_home", "total") and r.line is None):
            continue
        q = Quote(None if r.key == "h2h" else float(r.line), r.price_american, r.book, r.source,
                  r.fetched_at)  # fmt: skip
        if name == "ml_away":
            away[(r.game_id, r.source, r.book)] = q
        else:
            out.setdefault(r.game_id, {})[name] = q
    for gid, d in out.items():
        home = d.get("ml_home")
        if home is not None:
            a = away.get((gid, home.source, home.book))
            if a is None:
                d.pop("ml_home")
            else:
                d["ml_away"] = a
    return out
