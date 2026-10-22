"""The M16 rules, fixed in MODEL.md ("M16") before any live data: which status changes qualify,
Signal 1 (news) and Signal 2 (soft vs sharp), quiet hours, and the lag-study move sizes.

Pure functions on plain numbers, so every threshold is tested without a network or database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from betdata.moneyline import no_vig

EASTERN = ZoneInfo("America/New_York")

# --- qualifying status changes -----------------------------------------------------------------
NBA_MIN_PLAY = 20.0  # rotation player with >= 20 expected minutes per game played
PROB_MOVE = 0.25  # the change moves his play probability by >= 0.25
NFL_SNAP_SHARE = {"QB": 0.50, "RB": 0.60, "WR": 0.60, "TE": 0.60}  # offensive snaps, last 4
NFL_OTHER_SHARE = 0.80  # offensive linemen and defensive starters

# --- Signal 1 -------------------------------------------------------------------------------------
SIDE_MOVE = 1.0  # the model's home margin moved by >= 1 point because of the news
SIDE_EDGE = {"nba": 3.0, "nfl": 2.5}  # |margin + book home spread| in the direction of the move
PROP_MOVE = 0.10  # projection moved by >= 10%
PROP_EDGE = 0.08  # P(over | no push) - no-vig over >= 8 pp in the direction of the move
PROP_MARKETS = {"nba": ("points", "rebounds", "assists", "threes"),
                "nfl": ("rec_yds", "rush_yds", "receptions")}  # fmt: skip

# --- Signal 2 -------------------------------------------------------------------------------------
SHARP_GAP = 0.03  # soft no-vig >= 3 pp below Pinnacle's for the side
PIN_MAX_AGE_MIN = 10
SOFT_MAX_AGE_MIN = 5

# --- alerts and lag -------------------------------------------------------------------------------
QUIET_FROM, QUIET_TO = time(23, 0), time(7, 0)  # Eastern; stored, not pushed
LAG_WINDOW_MIN = 60
LAG_POINTS = 0.5  # a spread or total moved by >= 0.5 points
LAG_PROB = 0.015  # a moneyline or prop no-vig moved by >= 1.5 pp
LAG_RULE_MIN = 5.0  # median soft-book lag under 5 minutes turns Signal 1 off


def qualifies_nba(min_play: float | None, prob_before: float, prob_after: float) -> bool:
    return (min_play or 0) >= NBA_MIN_PLAY and abs(prob_after - prob_before) >= PROB_MOVE


def qualifies_nfl(position: str | None, snap_share: float | None, prob_before: float,
                  prob_after: float, *, starter: bool = False) -> bool:  # fmt: skip
    """`starter`: an offensive lineman or defensive starter (needs >= 80% of snaps)."""
    need = NFL_SNAP_SHARE.get((position or "").upper())
    if need is None:
        need = NFL_OTHER_SHARE if starter else None
    if need is None or snap_share is None:
        return False
    return snap_share >= need and abs(prob_after - prob_before) >= PROB_MOVE


def decimal(american: int) -> float:
    return 1 + american / 100 if american > 0 else 1 + 100 / -american


def quiet(now: datetime) -> bool:
    t = now.astimezone(EASTERN).time()
    return t >= QUIET_FROM or t < QUIET_TO


@dataclass(frozen=True)
class Spread:
    """One book's current full-game spread: the home line and both prices."""

    book: str
    home_line: float
    home_price: int
    away_price: int


@dataclass(frozen=True)
class Pick:
    side: str  # home, away, over, under
    line: float | None
    price: int
    book: str
    fair_prob: float | None
    book_prob: float | None
    edge: float  # points (side) or probability (props, Signal 2)


def side_signal(sport: str, margin_before: float, margin_after: float,
                spreads: list[Spread]) -> Pick | None:  # fmt: skip
    """Signal 1 on the spread: the news moved the model's home margin by >= 1 point and, at a
    DK or FD line, |margin + home spread| >= 3.0 (NBA) / 2.5 (NFL) in the direction of the move.
    The largest edge wins; a tie goes to the better price."""
    move = margin_after - margin_before
    if abs(move) < SIDE_MOVE:
        return None
    best = None
    for s in spreads:
        edge = margin_after + s.home_line  # > 0: home covers by this much
        if abs(edge) < SIDE_EDGE[sport] or (edge > 0) != (move > 0):
            continue
        side = "home" if edge > 0 else "away"
        price = s.home_price if side == "home" else s.away_price
        nv = no_vig(s.home_price, s.away_price)
        cand = Pick(side, s.home_line if side == "home" else -s.home_line, price, s.book, None,
                    (nv[0] if side == "home" else nv[1]) if nv else None, round(abs(edge), 2))
        if best is None or (cand.edge, decimal(cand.price)) > (best.edge, decimal(best.price)):
            best = cand
    return best


def prop_signal(mean_before: float, mean_after: float, p_over_after: float, line: float,
                over_price: int | None, under_price: int | None,
                book: str) -> Pick | None:  # fmt: skip
    """Signal 1 on a player prop: projection moved >= 10% and the model's P(over | no push) is
    >= 8 pp past the no-vig over in the direction of the move. No prices, no alert."""
    if over_price is None or under_price is None or mean_before <= 0:
        return None
    move = mean_after / mean_before - 1
    if abs(move) < PROP_MOVE:
        return None
    nv = no_vig(over_price, under_price)
    if nv is None:
        return None
    if move > 0 and p_over_after - nv[0] >= PROP_EDGE:
        return Pick("over", line, over_price, book, p_over_after, nv[0], p_over_after - nv[0])
    if move < 0 and nv[0] - p_over_after >= PROP_EDGE:
        return Pick("under", line, under_price, book, 1 - p_over_after, nv[1],
                    nv[0] - p_over_after)
    return None


@dataclass(frozen=True)
class TwoWay:
    """One book's two-way market at one line: (side a, side b) prices, e.g. home/away or
    over/under; `line` is the home spread or the total (None for a moneyline)."""

    book: str
    line: float | None
    price_a: int
    price_b: int
    age_min: float  # minutes since the poll that read it


def sharp_signal(pin: TwoWay, soft: TwoWay, sides=("home", "away")) -> list[Pick]:
    """Signal 2: same market and line; the soft book's no-vig for a side is >= 3 pp below
    Pinnacle's, and EV at the soft price with Pinnacle's no-vig probability is > 0. Quotes too
    old are not compared."""
    if pin.age_min > PIN_MAX_AGE_MIN or soft.age_min > SOFT_MAX_AGE_MIN or pin.line != soft.line:
        return []
    p, s = no_vig(pin.price_a, pin.price_b), no_vig(soft.price_a, soft.price_b)
    if p is None or s is None:
        return []
    out = []
    for i, (side, price) in enumerate(zip(sides, (soft.price_a, soft.price_b), strict=True)):
        line = soft.line if (soft.line is None or i == 0 or sides[0] == "over") else -soft.line
        if p[i] - s[i] >= SHARP_GAP and p[i] * decimal(price) - 1 > 0:
            out.append(Pick(side, line, price, soft.book, p[i], s[i], p[i] - s[i]))
    return out


def moved(market: str, line0: float | None, nv0: float | None, line1: float | None,
          nv1: float | None) -> bool:  # fmt: skip
    """The lag study's "the book moved" (MODEL.md, fixed): a spread or total line by >= 0.5
    points; a moneyline or prop by >= 1.5 pp of no-vig probability."""
    if market in ("spreads", "totals"):
        return line0 is not None and line1 is not None and abs(line1 - line0) >= LAG_POINTS
    return nv0 is not None and nv1 is not None and abs(nv1 - nv0) >= LAG_PROB
