"""Reading stored prop lines (M15)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
from sqlalchemy import Connection, select

from betdata.db.schema import books, prop_lines

BOOK_ORDER = ("draftkings", "espnbet", "fanduel", "betmgm", "caesars")


def latest_lines(conn: Connection, game_ids, at: datetime) -> pd.DataFrame:
    """Per (game, player, market): the newest live line known at `at`, from the most preferred
    book that has one. Columns: game_id, player_id, player_name, market, line, over_price,
    under_price, book, book_id, fetched_at, source."""
    ids = [int(g) for g in game_ids]
    cols = ["game_id", "player_id", "player_name", "market", "line", "over_price",
            "under_price", "book", "book_id", "fetched_at", "source"]  # fmt: skip
    if not ids:
        return pd.DataFrame(columns=cols)
    df = pd.read_sql(select(prop_lines.c.game_id, prop_lines.c.player_id,
                            prop_lines.c.player_name, prop_lines.c.market, prop_lines.c.line,
                            prop_lines.c.over_price, prop_lines.c.under_price,
                            books.c.key.label("book"), prop_lines.c.book_id,
                            prop_lines.c.fetched_at, prop_lines.c.source)
                     .join(books, books.c.book_id == prop_lines.c.book_id)
                     .where(prop_lines.c.game_id.in_(ids), prop_lines.c.kind == "poll",
                            prop_lines.c.fetched_at <= at), conn)  # fmt: skip
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["fetched_at"] = pd.to_datetime(df.fetched_at, utc=True)
    df["rank"] = df.book.map({b: i for i, b in enumerate(BOOK_ORDER)}).fillna(len(BOOK_ORDER))
    df = df.sort_values(["rank", "fetched_at"], ascending=[True, False])
    return df.drop_duplicates(["game_id", "player_id", "market"])[cols].reset_index(drop=True)
