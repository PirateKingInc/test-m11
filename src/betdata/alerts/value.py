"""M18 Part C: news -> reprice -> compare (MODEL.md "M18 Part C"). The value signal.

A qualifying status change re-predicts the game with the frozen models. The repriced fair
price is the pre-news consensus moved by the model's change:
- **Game markets:** L* - dmargin; T* + dtotal; the home win probability + d(Phi(margin/sigma)).
- **Props:** consensus_before(L) + p_model_after(L) - p_model_before(L).

Without a pre-news consensus, the model's own number is used ("model only").

**Pre-news consensus.** The consensus as of the latest poll answered at or before the report's
stated time (within PRE_WINDOW). Sweeps are hourly, so "fresh" is judged at that poll, not at
the stated time.

**Alert rule** (each soft book S in DK/FD, each market, side and player):
1. **Stale:** S's current line and prices equal its last quote at or before the stated time.
2. **Edge:** fair(side at S's line) minus S's no-vig >= 6 pp props, 3 pp game markets.
3. **Two-book or model only:** the other book or the consensus has already moved toward the
   side since the stated time, or the edge clears 9 / 4.5 pp.
4. **Dedup:** one alert per (game, market, player, side, change); the best edge across DK/FD.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from statistics import NormalDist

from sqlalchemy import Connection, select

from betdata.alerts import consensus as C
from betdata.alerts.db import poll_marks, quotes
from betdata.alerts.feeds import Quote
from betdata.alerts.grade import sigma, utc
from betdata.moneyline import no_vig

SOFT = ("draftkings", "fanduel")
EDGE = {"game": 0.03, "prop": 0.06}
EDGE_SOLO = {"game": 0.045, "prop": 0.09}  # 1.5x for two-book or model-only prices
PRE_WINDOW = timedelta(hours=3)
FIRST = {"spreads": "home", "h2h": "home"}  # props and totals: "over"
N = NormalDist()


def _first(market: str) -> str:
    return FIRST.get(market, "over")


def _other(side: str) -> str:
    return {"home": "away", "away": "home", "over": "under", "under": "over"}[side]


def side_line(market: str, side: str, first_line: float | None) -> float | None:
    """The side's own line from the first side's (home spread / total / prop line)."""
    if first_line is None:
        return None
    return -first_line if market == "spreads" and side == "away" else first_line


@dataclass(frozen=True)
class Pair:
    """One book's two-way quote: the first side's line (home spread, total, prop line) and both
    prices."""

    book: str
    line: float | None
    first_price: int
    second_price: int

    def novig(self, side: str, market: str) -> float | None:
        nv = no_vig(self.first_price, self.second_price)
        if nv is None:
            return None
        return nv[0] if side == _first(market) else nv[1]

    def price(self, side: str, market: str) -> int:
        return self.first_price if side == _first(market) else self.second_price


def pair_from(rows: dict[str, tuple], book: str, market: str) -> Pair | None:
    """rows: outcome -> (line, price)."""
    f, s = rows.get(_first(market)), rows.get(_other(_first(market)))
    if f is None or s is None or f[1] is None or s[1] is None:
        return None
    return Pair(book, f[0], int(f[1]), int(s[1]))


def current_pairs(qs: list[Quote], market: str, player_key: str | None) -> dict[str, Pair]:
    from betdata.alerts.poller import _norm

    rows: dict[str, dict[str, tuple]] = {}
    for q in qs:
        if q.market != market or q.book not in SOFT:
            continue
        if (_norm(q.player_name) if q.player_name else None) != player_key:
            continue
        rows.setdefault(q.book, {})[q.outcome] = (q.line, q.price)
    return {b: p for b, r in rows.items() if (p := pair_from(r, b, market)) is not None}


def pair_at(p: Connection, game_id: int, market: str, player_key: str | None, book: str,
            t: datetime) -> Pair | None:  # fmt: skip
    """The book's two-way quote as stored at or before `t` (latest row per side; a removal row
    means not listed)."""
    from betdata.alerts.poller import _norm

    q = quotes
    rows: dict[str, tuple] = {}
    for r in p.execute(select(q.c.outcome, q.c.line, q.c.price, q.c.player_name)
                       .where(q.c.game_id == game_id, q.c.market == market, q.c.book == book,
                              q.c.polled_at <= t,
                              q.c.player_name.is_(None) if player_key is None
                              else q.c.player_name.is_not(None))
                       .order_by(q.c.polled_at, q.c.quote_id)):  # fmt: skip
        if (_norm(r.player_name) if r.player_name else None) == player_key:
            rows[r.outcome] = (r.line, r.price)
    return pair_from(rows, book, market)


def pre_news_time(p: Connection, game_id: int, stated: datetime) -> datetime | None:
    t = p.execute(select(poll_marks.c.polled_at).where(
        poll_marks.c.game_id == game_id, poll_marks.c.polled_at <= stated,
        poll_marks.c.polled_at > stated - PRE_WINDOW)
        .order_by(poll_marks.c.polled_at.desc()).limit(1)).scalar()  # fmt: skip
    if t is None:
        t = p.execute(select(quotes.c.polled_at).where(
            quotes.c.game_id == game_id, quotes.c.polled_at <= stated,
            quotes.c.polled_at > stated - PRE_WINDOW)
            .order_by(quotes.c.polled_at.desc()).limit(1)).scalar()  # fmt: skip
    return None if t is None else utc(t)


def carried(sport: str, market: str, line: float | None, p_first: float, side: str,
            at_line: float | None) -> float | None:  # fmt: skip
    """A book's no-vig probability of `side`, carried from its own line to `at_line` (the
    side's own line) on the fair scale (sigma per market; props use the prop sigma)."""
    if market == "h2h":
        return p_first if side == "home" else 1 - p_first
    sig = sigma(sport, market, line)
    if sig is None or line is None or at_line is None:
        return None
    z = N.inv_cdf(min(max(p_first, 1e-6), 1 - 1e-6))
    if market == "spreads":
        fair = line - sig * z  # L*
        home_at = at_line if side == "home" else -at_line
        p = N.cdf((home_at - fair) / sig)
        return p if side == "home" else 1 - p
    fair = line + sig * z  # T* (or the prop's fair median)
    p = N.cdf((fair - at_line) / sig)
    return p if side == "over" else 1 - p


@dataclass
class Repriced:
    """The repriced fair price of one market (and player)."""

    sport: str
    market: str
    cons_before: C.Consensus | None = None  # game markets: the pre-news consensus
    shift: float = 0.0  # game markets: added to the consensus fair value
    model_fair: float | None = None  # game markets: the model's L*, T* or p_home
    model_fair_before: float | None = None  # the same before the news (model-only base)
    prop_cons: Callable | None = None  # props: first-side line -> pre-news Consensus | None
    prop_p_before: Callable | None = None  # props: line -> model P(over) before the news
    prop_p_after: Callable | None = None  # props: line -> model P(over) after the news

    def price(self, side: str, line: float | None) -> tuple[float | None, C.Consensus | None]:
        """(fair probability of the side at its line, the pre-news consensus used; None when
        the price is model only)."""
        if self.market in C.GAME:
            if self.cons_before is None:
                c = C.Consensus(self.sport, 0, self.market, datetime.min, "model",
                                self.model_fair)  # fmt: skip
                return c.prob(side, line), None
            c = replace(self.cons_before, fair=self.cons_before.fair + self.shift)
            if self.market == "h2h":
                c.fair = min(max(c.fair, 1e-6), 1 - 1e-6)
            return c.prob(side, line), self.cons_before
        after = self.prop_p_after(line)
        if after is None:
            return None, None
        cons = self.prop_cons(line)
        if cons is None:
            p_over = after
        else:
            base, before = cons.prob("over", line), self.prop_p_before(line)
            if base is None or before is None:
                return None, None
            p_over = min(max(base + after - before, 1e-6), 1 - 1e-6)
        return (p_over if side == "over" else 1 - p_over), cons


    def base(self, side: str, line: float | None) -> float | None:
        """The side's probability at its line without the news: the pre-news consensus (or,
        model only, the model before the news). fair - base is what the repricer moved."""
        if self.market in C.GAME:
            if self.cons_before is None:
                if self.model_fair_before is None:
                    return None
                c = C.Consensus(self.sport, 0, self.market, datetime.min, "model",
                                self.model_fair_before)  # fmt: skip
                return c.prob(side, line)
            return self.cons_before.prob(side, line)
        cons = self.prop_cons(line)
        p_over = cons.prob("over", line) if cons is not None else self.prop_p_before(line)
        if p_over is None:
            return None
        return p_over if side == "over" else 1 - p_over


def edge_source(news_move: float | None, edge: float | None) -> str | None:
    """Ledger tag of a C-signal pick: "news" when the repricer's move supplies at least half
    the edge, else "book disagreement" (the book was off the consensus before the news)."""
    if news_move is None or edge is None:
        return None
    return "news" if news_move >= edge / 2 else "book disagreement"


@dataclass
class Candidate:
    market: str
    player_name: str | None
    side: str
    book: str
    line: float | None
    price: int
    fair: float
    book_prob: float
    edge: float
    cons_before: C.Consensus | None  # None: model only
    confirmed: str | None  # what moved the same way, for a two-book or model-only price
    details: dict | None = None  # props: the projection before and after
    news_move: float | None = None  # fair minus the no-news base (Repriced.base)

    @property
    def solo(self) -> bool:
        return self.cons_before is None or self.cons_before.two_book


def evaluate(p: Connection, main: Connection | None, sport: str, game_id: int, market: str,
             player_name: str | None, rep: Repriced, now_qs: list[Quote], stated: datetime,
             now: datetime) -> Candidate | None:  # fmt: skip
    """The best qualifying candidate of one market (and player) across DK/FD and both sides,
    or None."""
    from betdata.alerts.poller import _norm

    key = _norm(player_name) if player_name else None
    kind = "prop" if player_name else "game"
    cur = current_pairs(now_qs, market, key)
    then = {b: pair_at(p, game_id, market, key, b, stated) for b in SOFT}

    def cons_now(first_line):
        if player_name is None:
            return C.game_consensus(p, main, sport, game_id, market, now)
        return C.prop_consensus(p, sport, game_id, market, player_name, now, first_line)

    best = None
    for book, q in cur.items():
        old = then.get(book)
        if old is None or old != q:  # 1. stale: unchanged since the stated time
            continue
        for side in (_first(market), _other(_first(market))):
            ln = side_line(market, side, q.line)
            fair, cons = rep.price(side, ln)
            bp = q.novig(side, market)
            if fair is None or bp is None:
                continue
            edge = fair - bp
            if edge < EDGE[kind]:  # 2. edge
                continue
            c = Candidate(market, player_name, side, book, ln, q.price(side, market), fair, bp,
                          edge, cons, None)  # fmt: skip
            base = rep.base(side, ln)
            c.news_move = None if base is None else fair - base
            if c.solo:  # 3. two-book or model only
                c.confirmed = _moved_toward(sport, market, side, ln, book, cur, then, cons,
                                            cons_now(q.line) if cons is not None else None)
                if c.confirmed is None and edge < EDGE_SOLO[kind]:
                    continue
            if best is None or c.edge > best.edge:
                best = c
    return best


def _moved_toward(sport, market, side, at_line, book, cur, then, cons_before,
                  cons_now) -> str | None:  # fmt: skip
    other = next(b for b in SOFT if b != book)
    o_now, o_then = cur.get(other), then.get(other)
    if o_now is not None and o_then is not None and o_now != o_then:
        a = _side_prob(sport, market, o_now, side, at_line)
        b = _side_prob(sport, market, o_then, side, at_line)
        if a is not None and b is not None and a > b:
            return f"{other} moved toward the side"
    if cons_before is not None and cons_now is not None:
        a, b = cons_now.prob(side, at_line), cons_before.prob(side, at_line)
        if a is not None and b is not None and a > b:
            return "the consensus moved toward the side"
    return None


def _side_prob(sport, market, pair: Pair, side, at_line) -> float | None:
    nv = no_vig(pair.first_price, pair.second_price)
    return None if nv is None else carried(sport, market, pair.line, nv[0], side, at_line)


def win_prob(sport: str, margin: float) -> float:
    """The model's home win probability from its margin (normal, the spread sigma)."""
    return N.cdf(margin / (sigma(sport, "spreads", None) or 12.0))


def game_repricing(sport: str, market: str, cons: C.Consensus | None, rp) -> Repriced | None:
    if market == "spreads":
        d, own = -(rp.margin_after - rp.margin_before), -rp.margin_after
    elif market == "totals":
        if rp.total_after is None or rp.total_before is None:
            return None
        d, own = rp.total_after - rp.total_before, rp.total_after
    else:
        d = win_prob(sport, rp.margin_after) - win_prob(sport, rp.margin_before)
        own = win_prob(sport, rp.margin_after)
    return Repriced(sport, market, cons_before=cons, shift=d, model_fair=own,
                    model_fair_before=own - d)  # the model's own number before the news


def minutes_since(stated: datetime, now: datetime) -> int:
    return max(0, math.floor((now - stated).total_seconds() / 60))
