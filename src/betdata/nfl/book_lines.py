"""The lines NFL picks are taken, shown and graded at (2026-10-28 decision, MODEL.md "NFL lines").

- **Pick and grade:** DraftKings and FanDuel only, best of the two for the side at pick time:
  the latest polled quote of each book at or before the prediction (`odds_snapshots`, any
  source). Spreads and totals: the better number for the side, then the better price;
  moneyline: the book whose no-vig price gives the side the larger edge, then the better price.
  No DK/FD quote at pick time: no pick.
- **Close (CLV):** Pinnacle's last pre-game quote of the side (the poller's `quotes`, then
  `odds_snapshots`), else the best of DK/FD's last pre-game quotes.

Lines are in sportsbook convention: the home spread is negative when the home team is favoured
(tests/unit/test_spread_convention.py).
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import Connection, column, create_engine, select, table

from betdata.core.normalize.odds_math import american_to_decimal
from betdata.db.schema import UTCDateTime, books, markets

PICK_BOOKS = ("draftkings", "fanduel")
CLOSE_BOOK = "pinnacle"
OTHER = {"home": "away", "away": "home", "over": "under", "under": "over"}

v_odds = table(
    "v_odds_snapshots",
    column("game_id"), column("market_id"), column("book_id"), column("game_outcome"),
    column("line"), column("price_american"), column("fetched_at", UTCDateTime),
    column("snapshot_kind"), column("source"), column("price_status"),
)  # fmt: skip


@dataclass(frozen=True)
class Quote:
    """One book's latest quote of one side."""

    book: str
    source: str
    line: float | None  # the side's own line (home/away spread, the total); None: moneyline
    price: int | None
    fetched_at: datetime


def _dec(price: int | None) -> float:
    return american_to_decimal(price) if price is not None and abs(price) >= 100 else 0.0


def better(side: str, market: str, q: Quote) -> tuple:
    """Sort key, larger = better for the bettor: more points on a spread, a lower over or a
    higher under, then the better price."""
    if market == "h2h" or q.line is None:
        pts = 0.0
    elif market == "totals":
        pts = -q.line if side == "over" else q.line
    else:
        pts = q.line
    return (pts, _dec(q.price))


class Board:
    """Every DK/FD poll quote of some games, read once; `at(game, t)` is what each book showed
    at `t`."""

    def __init__(self, conn: Connection, game_ids, books_=PICK_BOOKS):
        ids = list(game_ids)
        self._rows: dict[tuple, list[tuple[datetime, Quote]]] = defaultdict(list)
        if not ids:
            return
        rows = conn.execute(
            select(v_odds.c.game_id, markets.c.key, v_odds.c.game_outcome, v_odds.c.line,
                   v_odds.c.price_american, v_odds.c.source, v_odds.c.fetched_at,
                   books.c.key.label("book"))
            .join(markets, markets.c.market_id == v_odds.c.market_id)
            .join(books, books.c.book_id == v_odds.c.book_id)
            .where(v_odds.c.game_id.in_(ids), v_odds.c.snapshot_kind == "poll",
                   v_odds.c.price_status == "active", books.c.key.in_(books_),
                   markets.c.period == "game", markets.c.key.in_(("spreads", "totals", "h2h")))
            .order_by(v_odds.c.fetched_at)
        ).all()  # fmt: skip
        for r in rows:
            if r.key != "h2h" and r.line is None:
                continue
            q = Quote(r.book, r.source, None if r.key == "h2h" else float(r.line),
                      None if r.price_american is None else int(r.price_american), r.fetched_at)
            self._rows[(r.game_id, r.key, r.book, r.game_outcome)].append((r.fetched_at, q))
        self._times = {k: [t for t, _ in v] for k, v in self._rows.items()}

    def quote(self, game_id: int, market: str, book: str, side: str, at: datetime,
              strict: bool = False) -> Quote | None:  # fmt: skip
        """The book's latest quote of the side at `at` (before it, if `strict`)."""
        k = (game_id, market, book, side)
        times = self._times.get(k) if self._rows else None
        if not times:
            return None
        i = (bisect.bisect_left if strict else bisect.bisect_right)(times, at)
        return self._rows[k][i - 1][1] if i else None

    def best(self, game_id: int, market: str, side: str, at: datetime,
             strict: bool = False) -> Quote | None:  # fmt: skip
        qs = [q for b in PICK_BOOKS
              if (q := self.quote(game_id, market, b, side, at, strict)) is not None
              and (market != "h2h" or q.price is not None)]  # fmt: skip
        return max(qs, key=lambda q: better(side, market, q)) if qs else None

    def pair(self, game_id: int, book: str, at: datetime,
             strict: bool = False) -> tuple[Quote, Quote] | None:  # fmt: skip
        """(home, away) moneyline of one book at `at`."""
        h = self.quote(game_id, "h2h", book, "home", at, strict)
        a = self.quote(game_id, "h2h", book, "away", at, strict)
        if h is None or a is None or h.price is None or a.price is None:
            return None
        return h, a


# --- the close -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Close:
    book: str
    line: float | None
    price: int | None
    other_price: int | None  # the other side's price at the same line (no-vig for moneyline)


def poller_conn(data_dir: Path | None = None):
    """Read-only connection to the poller's database when it is on disk (the hourly job
    restores it before the run, never saves it); None otherwise."""
    from betdata.nfl.workflow import data_dir as default_dir

    path = Path(data_dir or default_dir()) / "poller.sqlite"
    if not path.exists():
        return None
    eng = create_engine(f"sqlite:///file:{path}?mode=ro&uri=true", future=True)
    return eng.connect()


def pinnacle_close(main: Connection, poller: Connection | None, game_id: int, market: str,
                   side: str, tip: datetime) -> Close | None:  # fmt: skip
    """Pinnacle's last quote of the side before `tip`, from the poller and the main store."""
    from betdata.alerts.grade import _main_obs, _poller_obs, latest_close

    a = SimpleNamespace(game_id=game_id, market=market, player_name=None)
    obs = _main_obs(main, a, CLOSE_BOOK, tip)
    if poller is not None:
        obs += _poller_obs(poller, a, CLOSE_BOOK, tip)
    c = latest_close(obs, side, market, CLOSE_BOOK)
    if c is None or (market == "h2h" and c.price is None):
        return None
    return Close(CLOSE_BOOK, c.line, c.price, c.other_price)


def book_close(board: Board, game_id: int, market: str, side: str, tip: datetime,
               book: str | None = None) -> Close | None:  # fmt: skip
    """DK/FD's last pre-game quote of the side: the best of the two (spreads, totals); for the
    moneyline the pick's own book if it has a close, else the other."""
    if market == "h2h":
        order = [book] + [b for b in PICK_BOOKS if b != book] if book else list(PICK_BOOKS)
        for b in order:
            p = board.pair(game_id, b, tip, strict=True)
            if p is not None:
                mine, other = (p[0], p[1]) if side == "home" else (p[1], p[0])
                return Close(b, None, mine.price, other.price)
        return None
    q = board.best(game_id, market, side, tip, strict=True)
    if q is None:
        return None
    o = board.quote(game_id, market, q.book, OTHER[side], tip, strict=True)
    return Close(q.book, q.line, q.price, o.price if o else None)


def close_for(main: Connection, poller: Connection | None, board: Board, game_id: int,
              market: str, side: str, tip: datetime,
              book: str | None = None) -> Close | None:  # fmt: skip
    return (pinnacle_close(main, poller, game_id, market, side, tip)
            or book_close(board, game_id, market, side, tip, book))  # fmt: skip


# --- the page ----------------------------------------------------------------------------------


def page_quotes(board: Board, game_id: int, now: datetime, margin: float | None,
                win_prob: float | None, spread_th: float, ml_t: float,
                calib=None) -> dict:  # fmt: skip
    """What a card shows (as `betdata.quotes.Quote`s, home-oriented spread): the DK/FD line a
    pick would be taken at (grade.book_bets at `now`); with no pick, DraftKings' line, else
    FanDuel's."""
    from betdata.nfl.grade import book_bets
    from betdata.quotes import Quote as Q

    def q(x: Quote | None) -> Q | None:
        return None if x is None else Q(x.line, x.price, x.book, x.source, x.fetched_at)

    bets = {b.market: b for b in book_bets(board, game_id, now, margin, None, win_prob,
                                            {"spread": spread_th, "total": None}, ml_t,
                                            calib)}  # fmt: skip
    out: dict = {}
    sb = bets.get("spread")
    order = [sb.book] if sb else list(PICK_BOOKS)
    for b in order:
        h = board.quote(game_id, "spreads", b, "home", now)
        if h is not None:
            out["spread_home"] = q(h)
            break
    mb = bets.get("moneyline")
    for b in [mb.book] if mb else list(PICK_BOOKS):
        pr = board.pair(game_id, b, now)
        if pr is not None:
            out["ml_home"], out["ml_away"] = q(pr[0]), q(pr[1])
            break
    return out
