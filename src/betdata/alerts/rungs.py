"""M18 Part G2: NFL alternate rungs priced from the M17 engine (MODEL.md "M18 Part G").

**Pricing:** the engine's simulated margin distribution (`margin_hist`, -60..60) and total
distribution (`total_hist`, 0..120), stored with each nfle17 prediction, price any rung, key
numbers 3 and 7 included.
- Spreads: P(home covers L | no push) = P(m > -L) / (1 - P(m = -L)).
- Totals: P(over T | no push), likewise.

**Paper alert** when the engine and the Pinnacle-implied full-game number agree and a DK/FD rung
disagrees with the engine by at least 4 pp.
- **Agree:** the engine's fair main number (median margin, median total) is within 1.5 points of
  Pinnacle's fair L* / T*.
- **Same direction:** Pinnacle's normal carry to the rung must also price the side above the
  book's no-vig.

**First-half rungs:** SharpAPI's free plan lists them, but the engine does not record
half-time scores, so they are skipped (logged in MODEL.md and BACKLOG).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from betdata.alerts.feeds import Quote
from betdata.moneyline import no_vig

EDGE = 0.04
AGREE_PTS = 1.5
SOFT = ("draftkings", "fanduel")


def cover_prob(hist, offset: int, side: str, line: float, market: str) -> float | None:
    """P(side wins at its own line | no push) from an integer histogram (value = index -
    offset): spreads use the home margin, totals the total."""
    h = np.asarray(hist, dtype=float)
    if h.sum() <= 0:
        return None
    p = h / h.sum()
    vals = np.arange(len(h)) - offset
    if market == "spreads":
        home_line = line if side == "home" else -line
        win, push = (vals + home_line > 0), (vals + home_line == 0)
        p_home = p[win].sum()
        p_push = p[push].sum()
        if p_push >= 1:
            return None
        ph = p_home / (1 - p_push)
        return ph if side == "home" else 1 - ph
    over, push = vals > line, vals == line
    po = p[over].sum() / (1 - p[push].sum()) if p[push].sum() < 1 else None
    if po is None:
        return None
    return po if side == "over" else 1 - po


def hist_median(hist, offset: int) -> float:
    h = np.asarray(hist, dtype=float)
    c = np.cumsum(h) / h.sum()
    return float(np.searchsorted(c, 0.5) - offset)


@dataclass(frozen=True)
class Rung:
    game_id: int
    book: str
    market: str  # spreads, totals
    side: str
    line: float  # the side's own line
    price: int
    book_prob: float
    engine_prob: float
    pin_prob: float | None

    @property
    def edge(self) -> float:
        return self.engine_prob - self.book_prob


def rung_pairs(qs: list[Quote]) -> list[tuple]:
    """(game, book, market, first-side line, first price, second price) of each two-way alt
    rung: home line L with away -L, or over/under at T."""
    by: dict[tuple, dict] = {}
    for q in qs:
        if q.book not in SOFT or q.line is None or q.price is None:
            continue
        m = q.market.removeprefix("alt_")
        key_line = q.line if (m == "totals" or q.outcome == "home") else -q.line
        by.setdefault((q.game_id, q.book, m, key_line), {})[q.outcome] = q.price
    out = []
    for (gid, book, m, ln), s in by.items():
        a, b = ("home", "away") if m == "spreads" else ("over", "under")
        if a in s and b in s:
            out.append((gid, book, m, ln, s[a], s[b]))
    return out


def judge(pairs: list[tuple], engine: dict, pin_fair: dict, pin_prob) -> list[Rung]:
    """engine: game -> {"margin_hist", "total_hist"}; pin_fair: (game, market) -> L* / T*;
    pin_prob(game, market, side, line) -> Pinnacle's carried probability."""
    out = []
    for gid, book, m, ln, pa, pb in pairs:
        e = engine.get(gid)
        fair = pin_fair.get((gid, m))
        if e is None or fair is None:
            continue
        hist = e.get("margin_hist" if m == "spreads" else "total_hist")
        if not hist:
            continue
        offset = 60 if m == "spreads" else 0
        med = hist_median(hist, offset)
        engine_fair = -med if m == "spreads" else med
        if abs(engine_fair - fair) > AGREE_PTS:
            continue
        nv = no_vig(pa, pb)
        if nv is None:
            continue
        for side, bp, price in ((("home", nv[0], pa), ("away", nv[1], pb)) if m == "spreads"
                                else (("over", nv[0], pa), ("under", nv[1], pb))):
            own = ln if (m == "totals" or side == "home") else -ln
            ep = cover_prob(hist, offset, side, own, m)
            if ep is None or ep - bp < EDGE:
                continue
            pp = pin_prob(gid, m, side, own)
            if pp is not None and pp < bp:
                continue
            out.append(Rung(gid, book, m, side, own, price, bp, ep, pp))
    return out
