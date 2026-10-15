from __future__ import annotations

import logging
import re

from sqlalchemy import Connection, insert, select

from betdata.db.schema import book_aliases, books, markets

log = logging.getLogger(__name__)


def book_key(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", name.lower()) or name


class BookResolver:
    """Maps a source's bookmaker key to our book_id. Unknown books are created, not dropped."""

    def __init__(self, conn: Connection, source: str):
        self.conn = conn
        self.source = source
        self._cache = {
            k: b
            for k, b in conn.execute(
                select(book_aliases.c.source_key, book_aliases.c.book_id).where(
                    book_aliases.c.source == source
                )
            )
        }

    def resolve(self, source_key: str, title: str | None = None) -> int:
        if source_key in self._cache:
            return self._cache[source_key]
        key = book_key(source_key)  # "ESPN Bet" -> "espnbet", matching our seeded key
        book_id = self.conn.execute(select(books.c.book_id).where(books.c.key == key)).scalar()
        if book_id is None:
            log.info("new book from %s: %s (%s)", self.source, source_key, title)
            book_id = self.conn.execute(
                insert(books).values(key=key, name=title or source_key)
            ).inserted_primary_key[0]
        self.conn.execute(
            insert(book_aliases).values(source=self.source, source_key=source_key, book_id=book_id)
        )
        self._cache[source_key] = book_id
        return book_id


class MarketResolver:
    def __init__(self, conn: Connection):
        self.conn = conn
        self._cache = {
            (k, p): m
            for m, k, p in conn.execute(
                select(markets.c.market_id, markets.c.key, markets.c.period)
            )
        }

    def resolve(self, key: str, period: str = "game") -> int:
        if (key, period) not in self._cache:
            self._cache[(key, period)] = self.conn.execute(
                insert(markets).values(key=key, period=period)
            ).inserted_primary_key[0]
        return self._cache[(key, period)]
