"""M15 NBA prop backtest helpers: one book's open/close lines per player-market, bets past an
edge threshold graded at the stored prices (push on a whole-number line), the record."""

from datetime import UTC, datetime

import numpy as np
import pandas as pd
from sqlalchemy import insert, select

from betdata.db.schema import books, prop_lines
from betdata.nba.props.select import bets, history_lines, record
from tests.nba.synth import synth


def test_history_lines_prefers_book_with_both_sides(engine):
    gid = synth(engine, n_days=2)[0]
    now = datetime(2022, 1, 1, tzinfo=UTC)
    with engine.begin() as conn:
        bk = dict(conn.execute(select(books.c.key, books.c.book_id)).all())
        rows = [("draftkings", "close", 20.5, -110, None), ("betmgm", "close", 21.5, -115, -105),
                ("betmgm", "open", 19.5, -110, -110)]  # fmt: skip
        for b, kind, line, o, u in rows:
            conn.execute(insert(prop_lines).values(
                source="espn_props", source_game_id=str(gid), game_id=gid, player_id="p1",
                market="points", book_id=bk[b], kind=kind, line=line, over_price=o,
                under_price=u, quoted_at=datetime(2021, 10, 1, tzinfo=UTC),
                fetched_at=now))  # fmt: skip
        df = history_lines(conn)
    r = df.iloc[0]
    assert (r.close_line, r.close_over_price, r.open_line) == (21.5, -115, 19.5)
    # A close quoted after tip (an in-game line, REVIEW M15-3) is dropped.
    with engine.begin() as conn:
        conn.execute(prop_lines.update().values(quoted_at=datetime(2022, 6, 1, tzinfo=UTC)))
        assert "close_line" not in history_lines(conn).columns


def test_history_lines_drops_lopsided_alternates(engine):
    # M16 audit (REVIEW M16-1): an alternate line stored as "main" (+300/-450) is not a main line.
    gid = synth(engine, n_days=2)[0]
    with engine.begin() as conn:
        bk = dict(conn.execute(select(books.c.key, books.c.book_id)).all())
        for b, line, o, u in [("draftkings", 14.5, 300, -450), ("betmgm", 10.5, -110, -110)]:
            conn.execute(insert(prop_lines).values(
                source="espn_props", source_game_id=str(gid), game_id=gid, player_id="p1",
                market="points", book_id=bk[b], kind="open", line=line, over_price=o,
                under_price=u, quoted_at=datetime(2021, 10, 1, tzinfo=UTC),
                fetched_at=datetime(2022, 1, 1, tzinfo=UTC)))  # fmt: skip
        df = history_lines(conn)
    assert (df.iloc[0].open_line, len(df)) == (10.5, 1)


def test_bets_and_record():
    d = pd.DataFrame({"edge": [6.0, -7.0, 2.0, 8.0], "over": [1.0, 1.0, 0.0, np.nan],
                      "over_price": [-110, -110, -110, +120],
                      "under_price": [-110, -110, -110, -140]})  # fmt: skip
    b = bets(d, 5.0)
    assert list(b.result) == ["win", "loss", "push"]
    r = record(b)
    assert (r["bets"], r["w"], r["l"], r["p"]) == (3, 1, 1, 1)
    assert abs(r["roi"] - (100 / 110 - 1) / 3) < 1e-4
