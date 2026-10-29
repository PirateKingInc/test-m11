"""The consensus price, the answer key of M18 (MODEL.md "M18 Part B").

**Fresh** means polled within the last 10 minutes. The inputs are:
- every quote in the poller's `quotes` (SharpAPI DK/FD, The Odds API Pinnacle, Kalshi);
- The Odds API boards in the main database's `odds_snapshots`, every bookmaker on them.

**Game markets** (spreads, totals, h2h):
- Pinnacle's no-vig if Pinnacle is fresh (method "pinnacle").
- Otherwise the median of every fresh book's no-vig (method "median"; Kalshi votes on the
  moneyline only). Each book's number is put on a common scale first: the fair home line
  L* = L - sigma*inv(p_home(L)), the fair total T* = T + sigma*inv(p_over(T)), or the home win
  probability.
- **"Two-book":** the only books are DK and FD (Kalshi aside).

**Props:**
- The median no-vig over probability across fresh books quoting one line: the given line, else
  the line most books quote. Pinnacle is not used.
- **"Two-book":** the only books are DK and FD.

`store` writes a consensus with its inputs to the poller table `consensus`.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from statistics import NormalDist

from sqlalchemy import Connection, column, insert, select, table

from betdata.alerts.db import consensus as consensus_t
from betdata.alerts.db import poll_marks, quotes
from betdata.alerts.grade import sigma, utc
from betdata.db.schema import UTCDateTime, books, markets
from betdata.moneyline import no_vig

FRESH = timedelta(minutes=10)
GAME = ("spreads", "totals", "h2h")
PIN, KALSHI = "pinnacle", "kalshi"
TWO_BOOKS = {"draftkings", "fanduel"}
N = NormalDist()

v_odds = table(
    "v_odds_snapshots",
    column("game_id"), column("market_id"), column("book_id"), column("game_outcome"),
    column("line"), column("price_american"), column("fetched_at", UTCDateTime),
    column("snapshot_kind"), column("source"), column("price_status"),
)  # fmt: skip


@dataclass(frozen=True)
class Input:
    source: str
    book: str
    line: float | None  # home line / total / prop line; None for h2h
    price: int  # home / over price
    other_price: int  # away / under price
    polled_at: datetime
    fair: float  # L*, T*, p_home, or the prop's no-vig over probability


@dataclass
class Consensus:
    sport: str
    game_id: int
    market: str
    at: datetime
    method: str  # "pinnacle" | "median"
    fair: float  # L*, T*, p_home, or (props) the over probability at `line`
    line: float | None = None  # props: the line the over probability is at
    player_name: str | None = None
    inputs: list[Input] = field(default_factory=list)

    @property
    def two_book(self) -> bool:
        return {i.book for i in self.inputs if i.book != KALSHI} <= TWO_BOOKS

    @property
    def n(self) -> int:
        return len(self.inputs)

    def prob(self, side: str, line: float | None = None) -> float | None:
        """The consensus probability that `side` wins at `line` (spread: the side's own line;
        total: the total; props: must be the consensus line)."""
        if self.market == "h2h":
            return self.fair if side == "home" else 1 - self.fair
        if self.market in ("spreads", "totals"):
            sig = sigma(self.sport, self.market, None)
            if line is None or sig is None:
                return None
            if self.market == "spreads":
                home_line = line if side == "home" else -line
                p = N.cdf((home_line - self.fair) / sig)
                return p if side == "home" else 1 - p
            p = N.cdf((self.fair - line) / sig)
            return p if side == "over" else 1 - p
        if line is not None and line != self.line:
            return None
        return self.fair if side == "over" else 1 - self.fair


def _clamp(p: float) -> float:
    return min(max(p, 1e-6), 1 - 1e-6)


def _scale(sport: str, market: str, line: float | None, p_first: float) -> float | None:
    """A book's number on the common scale, from its no-vig probability of the first side
    (home / over) at its line."""
    if market == "h2h":
        return p_first
    sig = sigma(sport, market, None)
    if sig is None or line is None:
        return None
    if market == "spreads":
        return line - sig * N.inv_cdf(_clamp(p_first))
    return line + sig * N.inv_cdf(_clamp(p_first))


def _fresh_rows(p: Connection | None, main: Connection | None, game_id: int, market: str,
                player_name: str | None, at: datetime) -> list[tuple]:  # fmt: skip
    """(source, book, outcome, line, price, polled_at) of every fresh quote."""
    out: list[tuple] = []
    lo = at - FRESH
    if p is not None:
        out += _poller_board(p, game_id, market, player_name, at)
    if main is not None and player_name is None:
        v = v_odds
        for r in main.execute(
                select(v.c.source, books.c.key.label("book"), v.c.game_outcome, v.c.line,
                       v.c.price_american, v.c.fetched_at)
                .join(markets, markets.c.market_id == v.c.market_id)
                .join(books, books.c.book_id == v.c.book_id)
                .where(v.c.game_id == game_id, markets.c.key == market,
                       markets.c.period == "game", v.c.snapshot_kind == "poll",
                       v.c.price_status == "active", v.c.source == "the_odds_api",
                       v.c.fetched_at > lo, v.c.fetched_at <= at)):  # fmt: skip
            out.append((r.source, r.book, r.game_outcome, r.line, r.price_american,
                        utc(r.fetched_at)))
    return out


def _poller_board(p: Connection, game_id: int, market: str, player_name: str | None,
                  at: datetime) -> list[tuple]:  # fmt: skip
    """Each (source, book)'s board as of its latest answer in the window: the newest
    `poll_marks` row (or, without marks, its newest quote) in (at - FRESH, at]; every key's
    latest row at or before then, removals dropped. Rows carry that answer time."""
    q, lo = quotes, at - FRESH
    cond = [q.c.game_id == game_id, q.c.market == market, q.c.polled_at <= at,
            q.c.player_name.is_(None) if player_name is None else q.c.player_name.is_not(None)]
    want = _key(player_name) if player_name else None
    rows = [r for r in p.execute(select(q.c.source, q.c.book, q.c.outcome, q.c.line, q.c.price,
                                        q.c.polled_at, q.c.player_name, q.c.quote_id)
                                 .where(*cond).order_by(q.c.polled_at, q.c.quote_id))
            if want is None or _key(r.player_name) == want]  # fmt: skip
    seen_at: dict[tuple[str, str], datetime] = {}
    for m in p.execute(select(poll_marks.c.source, poll_marks.c.book, poll_marks.c.polled_at)
                       .where(poll_marks.c.game_id == game_id, poll_marks.c.polled_at > lo,
                              poll_marks.c.polled_at <= at)):  # fmt: skip
        k, t = (m.source, m.book), utc(m.polled_at)
        seen_at[k] = max(seen_at.get(k, t), t)
    for r in rows:
        t = utc(r.polled_at)
        if t > lo:
            k = (r.source, r.book)
            seen_at[k] = max(seen_at.get(k, t), t)
    state: dict[tuple, tuple] = {}
    for r in rows:
        k = (r.source, r.book)
        if k in seen_at and utc(r.polled_at) <= seen_at[k]:
            state[(r.source, r.book, r.outcome)] = r  # one main line per side (change-only key)
    out = []
    for (src, book, outcome), r in state.items():
        if r.price is not None:
            out.append((src, book, outcome, r.line, r.price, seen_at[(src, book)]))
    return out


def _key(name: str | None) -> str:
    from betdata.alerts.poller import _norm

    return _norm(name) if name else ""


def _pairs(rows: list[tuple], market: str) -> dict[tuple[str, str], tuple]:
    """Per (source, book): its latest two-way pair at one line,
    (line, first price, second price, polled_at), first = home/over."""
    first, second = ("home", "away") if market in ("spreads", "h2h") else ("over", "under")
    by: dict[tuple[str, str], list[tuple]] = {}
    for r in rows:
        by.setdefault((r[0], r[1]), []).append(r)
    out = {}
    for k, rs in by.items():
        rs.sort(key=lambda r: r[5], reverse=True)
        a = next((r for r in rs if r[2] == first and r[4] is not None), None)
        if a is None:
            continue
        want = None if market == "h2h" else (-a[3] if market == "spreads" else a[3])
        b = next((r for r in rs if r[2] == second and r[4] is not None
                  and (market == "h2h" or (r[3] is not None and a[3] is not None
                                           and abs(r[3] - want) < 1e-9))), None)  # fmt: skip
        if b is None:
            continue
        out[k] = (a[3], int(a[4]), int(b[4]), max(a[5], b[5]))
    return out


def game_consensus(p: Connection | None, main: Connection | None, sport: str, game_id: int,
                   market: str, at: datetime) -> Consensus | None:  # fmt: skip
    rows = _fresh_rows(p, main, game_id, market, None, at)
    inputs = []
    for (src, book), (line, pa, pb, t) in _pairs(rows, market).items():
        if book == KALSHI and market != "h2h":
            continue
        nv = no_vig(pa, pb)  # Kalshi: the two teams' yes asks, normalised the same way
        if nv is None:
            continue
        f = _scale(sport, market, line, nv[0])
        if f is not None:
            inputs.append(Input(src, book, line, pa, pb, t, f))
    if not inputs:
        return None
    pins = [i for i in inputs if i.book == PIN]
    if pins:
        best = max(pins, key=lambda i: i.polled_at)
        return Consensus(sport, game_id, market, at, "pinnacle", best.fair, inputs=[best])
    # one vote per book: the freshest of its sources
    per_book: dict[str, Input] = {}
    for i in sorted(inputs, key=lambda i: i.polled_at):
        per_book[i.book] = i
    used = list(per_book.values())
    return Consensus(sport, game_id, market, at, "median",
                     statistics.median(i.fair for i in used), inputs=used)


def prop_consensus(p: Connection, sport: str, game_id: int, market: str, player_name: str,
                   at: datetime, line: float | None = None) -> Consensus | None:  # fmt: skip
    rows = _fresh_rows(p, None, game_id, market, player_name, at)
    pairs = {k: v for k, v in _pairs(rows, market).items() if k[1] != PIN}
    per_book: dict[str, Input] = {}
    for (src, book), (ln, po, pu, t) in sorted(pairs.items(), key=lambda kv: kv[1][3]):
        nv = no_vig(po, pu)
        if nv is not None and ln is not None:
            per_book[book] = Input(src, book, ln, po, pu, t, nv[0])
    if not per_book:
        return None
    if line is None:
        counts: dict[float, int] = {}
        for i in per_book.values():
            counts[i.line] = counts.get(i.line, 0) + 1
        line = min(counts, key=lambda ln: (-counts[ln], ln))
    used = [i for i in per_book.values() if i.line == line]
    if not used:
        return None
    return Consensus(sport, game_id, market, at, "median",
                     statistics.median(i.fair for i in used), line=line,
                     player_name=player_name, inputs=used)


def store(p: Connection, c: Consensus) -> int:
    return p.execute(insert(consensus_t).values(
        sport=c.sport, game_id=c.game_id, market=c.market, player_name=c.player_name,
        at=c.at, method=c.method, two_book=c.two_book, n=c.n, fair=c.fair, line=c.line,
        inputs=[{**asdict(i), "polled_at": i.polled_at.isoformat()} for i in c.inputs],
    )).inserted_primary_key[0]  # fmt: skip
