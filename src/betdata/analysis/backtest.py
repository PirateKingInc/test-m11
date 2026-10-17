"""Walk-forward backtests against closing spreads.

No look-ahead: games are processed one calendar day (US Eastern) at a time. Every game that
day is predicted before any of that day's results reach the model, so a 4 pm prediction never
sees the 1 pm scores (conservative: a real bettor could). Parameters and the bet threshold
are chosen on training seasons only; test seasons are scored once with those choices.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from betdata.analysis.dataset import GameRow
from betdata.analysis.models import EloModel, Model, Prediction
from betdata.core.normalize.odds_math import american_to_decimal

EASTERN = ZoneInfo("America/New_York")
DEFAULT_PRICE = -110
BREAK_EVEN = 110 / 210  # win rate needed at -110: 52.38%


def walk_forward(games: Sequence[GameRow], model: Model) -> list[tuple[GameRow, Prediction | None]]:
    """Predict every game, then let the model learn its result, a day at a time."""
    ordered = sorted(games, key=lambda g: (g.start, g.game_id))
    out: list[tuple[GameRow, Prediction | None]] = []
    for _, day in itertools.groupby(ordered, key=lambda g: g.start.astimezone(EASTERN).date()):
        day = list(day)
        out += [(g, model.predict(g)) for g in day]
        for g in day:
            model.update(g)
    return out


@dataclass(frozen=True)
class Pick:
    game: GameRow
    pred_margin: float
    edge: float  # model margin minus market margin, in points, from the home side
    side: str  # home or away
    line: float  # the side's spread
    price: int
    result: str  # win, loss, push
    profit: float  # per 1 unit staked


def grade(game: GameRow, side: str) -> tuple[float, str]:
    """(line, result) for a spread bet on `side` at the closing line."""
    line = game.spread_home if side == "home" else -game.spread_home
    margin = game.home_margin if side == "home" else -game.home_margin
    cover = margin + line
    return line, "win" if cover > 0 else "loss" if cover < 0 else "push"


def profit(result: str, price: int) -> float:
    if result == "win":
        return american_to_decimal(price) - 1
    return -1.0 if result == "loss" else 0.0


def picks(
    preds: Iterable[tuple[GameRow, Prediction | None]],
    threshold: float,
    seasons: Iterable[int] | None = None,
    regular_only: bool = False,
) -> list[Pick]:
    """Bet the side the model favours wherever it disagrees with the close by >= threshold."""
    keep = set(seasons) if seasons is not None else None
    out = []
    for g, p in preds:
        if p is None or not g.final or g.spread_home is None:
            continue
        if keep is not None and g.season not in keep:
            continue
        if regular_only and g.season_type != "REG":
            continue
        edge = p.home_margin + g.spread_home  # market expects the home team to win by -spread
        if threshold <= 0 or abs(edge) < threshold:
            continue
        side = "home" if edge > 0 else "away"
        price = (g.spread_home_price if side == "home" else g.spread_away_price) or DEFAULT_PRICE
        line, result = grade(g, side)
        out.append(Pick(g, p.home_margin, edge, side, line, price, result, profit(result, price)))
    return out


@dataclass(frozen=True)
class Summary:
    bets: int
    wins: int
    losses: int
    pushes: int
    profit: float

    @property
    def decided(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.decided if self.decided else None

    @property
    def roi(self) -> float | None:
        return self.profit / self.bets if self.bets else None

    def ci95(self) -> tuple[float, float] | None:
        return wilson(self.wins, self.decided)

    def p_value(self) -> float | None:
        """Chance of at least this many wins if the true win rate were break-even."""
        return binomial_tail(self.wins, self.decided, BREAK_EVEN) if self.decided else None


def summarize(ps: Iterable[Pick]) -> Summary:
    ps = list(ps)
    count = {r: sum(p.result == r for p in ps) for r in ("win", "loss", "push")}
    return Summary(len(ps), count["win"], count["loss"], count["push"], sum(p.profit for p in ps))


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = wins / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return centre - half, centre + half


def binomial_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), exact (log space, fine for any season count)."""
    if k <= 0:
        return 1.0
    log_p, log_q = math.log(p), math.log1p(-p)
    terms = [
        math.lgamma(n + 1)
        - math.lgamma(i + 1)
        - math.lgamma(n - i + 1)
        + i * log_p
        + (n - i) * log_q
        for i in range(k, n + 1)
    ]
    top = max(terms)
    return min(1.0, math.exp(top) * sum(math.exp(t - top) for t in terms))


def mae(
    preds: Iterable[tuple[GameRow, Prediction | None]], seasons: Iterable[int]
) -> tuple[float | None, float | None, int]:
    """(model error, closing-line error, games): mean absolute miss on the final margin, over
    final games that have both a prediction and a closing line."""
    keep = set(seasons)
    rows = [
        (abs(p.home_margin - g.home_margin), abs(-g.spread_home - g.home_margin))
        for g, p in preds
        if p is not None and g.final and g.spread_home is not None and g.season in keep
    ]
    if not rows:
        return None, None, 0
    return sum(r[0] for r in rows) / len(rows), sum(r[1] for r in rows) / len(rows), len(rows)


# --- Choosing parameters on training seasons ---------------------------------

ELO_GRID = {
    "k": (15.0, 20.0, 25.0, 30.0),
    "hfa": (40.0, 50.0, 60.0, 70.0),
    "revert": (0.25, 0.33, 0.5),
    "bye": (0.0, 25.0),
}
THRESHOLDS = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0)
MIN_TRAIN_BETS = 200  # a threshold that bets less often than this on training is not chosen


@dataclass(frozen=True)
class Tuned:
    params: dict
    train_mae: float
    threshold: float
    by_threshold: list[tuple[float, Summary]]  # training result for each candidate


def tune(
    games: Sequence[GameRow],
    train: Sequence[int],
    make: Callable[..., Model] = EloModel,
    grid: dict[str, Sequence[float]] | None = None,
    thresholds: Sequence[float] = THRESHOLDS,
    regular_only: bool = False,
) -> Tuned:
    """Pick parameters by the smallest error on the final margin over training seasons (an
    honest fit, not tuned for betting), then the bet threshold with the best training ROI
    among thresholds that bet at least MIN_TRAIN_BETS times. Test seasons are never read."""
    train = set(train)
    grid = ELO_GRID if grid is None else grid
    keys = sorted(grid)
    best: tuple[float, dict, list] | None = None
    for values in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, values, strict=True))
        preds = walk_forward(games, make(**params))
        err, _, n = mae(preds, train)
        if n and (best is None or err < best[0]):
            best = (err, params, preds)
    if best is None:
        raise ValueError("no training games with a closing line and a final score")
    err, params, preds = best
    by_threshold = [(t, summarize(picks(preds, t, train, regular_only))) for t in thresholds]
    eligible = [(t, s) for t, s in by_threshold if s.bets >= MIN_TRAIN_BETS]
    if eligible:
        threshold = max(eligible, key=lambda ts: (ts[1].roi, -ts[0]))[0]
    else:
        threshold = min(thresholds)
    return Tuned(params, err, threshold, by_threshold)


def verdict(test: Summary) -> str:
    rate = test.win_rate
    if rate is None or test.decided < 300:
        return "INCONCLUSIVE: fewer than 300 decided test bets"
    if rate >= 0.54:
        return "EDGE: above 54% on unseen seasons; worth live paper-trading"
    if rate <= BREAK_EVEN:
        return "NO EDGE: at or below the 52.4% break-even on unseen seasons"
    return "INCONCLUSIVE: above break-even but below 54%; could be luck"
