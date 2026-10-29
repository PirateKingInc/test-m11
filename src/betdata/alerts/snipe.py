"""M18 Part F: opening-line prop snipes, NBA rebounds and assists (MODEL.md "M18 Part F").

When a DK or FD rebound or assist prop first posts (both sides new in `openers`), nbap15 prices
that line the moment it posts: P(over | no push) from its distribution at the projection, with
the validated calibration. A side is alerted when:
- its probability beats the book's no-vig by at least 6 pp; and
- if other books already quote that line, their consensus does not side against the model:
  its no-vig probability of the side is not below the book's.

One alert per opener (game, book, player, market, line). This is its own signal ("snipe", F),
with its own 200-alert gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from betdata.alerts.feeds import Quote
from betdata.moneyline import no_vig

MARKETS = ("rebounds", "assists")
SOFT = ("draftkings", "fanduel")
EDGE = 0.06


@dataclass(frozen=True)
class Opener:
    game_id: int
    book: str
    player_name: str
    market: str
    line: float
    over: int
    under: int


def openers_of(new: list[Quote]) -> list[Opener]:
    """Two-way NBA rebound/assist openers at DK/FD (over and under first seen together)."""
    from betdata.alerts.poller import _norm

    sides: dict[tuple, dict[str, Quote]] = {}
    for q in new:
        if (q.sport != "nba" or q.market not in MARKETS or q.book not in SOFT
                or not q.player_name or q.price is None or q.line is None):  # fmt: skip
            continue
        sides.setdefault((q.game_id, q.book, _norm(q.player_name), q.market), {})[q.outcome] = q
    out = []
    for (gid, book, _k, market), s in sides.items():
        o, u = s.get("over"), s.get("under")
        if o is None or u is None or o.line != u.line:
            continue
        out.append(Opener(gid, book, o.player_name, market, float(o.line), int(o.price),
                          int(u.price)))  # fmt: skip
    return out


@dataclass(frozen=True)
class Snipe:
    side: str
    price: int
    p_model: float
    p_book: float
    edge: float
    p_cons: float | None  # the other books' consensus of the side at the line, if any


def judge(op: Opener, p_over: float, cons_over: float | None) -> Snipe | None:
    nv = no_vig(op.over, op.under)
    if nv is None:
        return None
    best = None
    for side, pm, pb, price in (("over", p_over, nv[0], op.over),
                                ("under", 1 - p_over, nv[1], op.under)):  # fmt: skip
        if pm - pb < EDGE:
            continue
        pc = None if cons_over is None else (cons_over if side == "over" else 1 - cons_over)
        if pc is not None and pc < pb:  # the other books side against the model
            continue
        s = Snipe(side, price, pm, pb, pm - pb, pc)
        if best is None or s.edge > best.edge:
            best = s
    return best
