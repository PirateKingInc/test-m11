"""Moneyline arithmetic shared by every sport: fair odds from a win probability, no-vig market
probabilities, edges, profit at American odds, and a one-parameter-pair (Platt) calibration.

All probabilities are 0-1 floats. Edges are reported in percentage points of win probability
(model 0.58 vs market 0.53 = +5.0).
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path


def fair_american(p: float) -> int | None:
    """American odds with no margin for an outcome of probability p: 0.6 -> -150, 0.4 -> +150."""
    if p is None or not 0.0 < p < 1.0:
        return None
    if p >= 0.5:
        return -round(100 * p / (1 - p))
    return round(100 * (1 - p) / p)


def implied(american: float) -> float:
    """Implied probability of American odds, vig included: -110 -> 0.524, +150 -> 0.4."""
    a = float(american)
    return -a / (-a + 100) if a < 0 else 100 / (a + 100)


def no_vig(home_price: float | None, away_price: float | None) -> tuple[float, float] | None:
    """The two sides' implied probabilities scaled to sum to 1 (proportional vig removal)."""
    if home_price is None or away_price is None:
        return None
    h, a = implied(home_price), implied(away_price)
    return h / (h + a), a / (h + a)


def edge_pts(model_p: float, market_p: float) -> float:
    return round(100 * (model_p - market_p), 2)


def profit(result: str, american: float, stake: float = 1.0) -> float:
    """Profit of a `stake` bet that went 'win' / 'loss' / 'push' / 'void' at these odds."""
    if result == "win":
        a = float(american)
        return stake * (a / 100 if a > 0 else 100 / -a)
    if result == "loss":
        return -stake
    return 0.0


def clv_pts(bet_novig: float | None, close_novig: float | None) -> float | None:
    """Closing-line value in implied-probability points: how much likelier the market made the
    backed side between the bet and the close. Positive = beat the close."""
    if bet_novig is None or close_novig is None:
        return None
    return round(100 * (close_novig - bet_novig), 2)


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


@dataclass
class Calibration:
    """p' = sigmoid(slope * logit(p) + intercept). slope < 1 shrinks an overconfident model
    toward 50%. The identity (1, 0) means "not applied"."""

    slope: float = 1.0
    intercept: float = 0.0
    fitted_on: str = ""
    applied: bool = False

    def __call__(self, p: float) -> float:
        if not self.applied:
            return p
        z = self.slope * _logit(p) + self.intercept
        return 1 / (1 + math.exp(-z))


def fit_platt(probs, outcomes, iters: int = 50) -> tuple[float, float]:
    """Logistic regression of the outcome on logit(p) (Newton's method); returns
    (slope, intercept)."""
    import numpy as np

    x = np.array([_logit(p) for p in probs])
    y = np.asarray(outcomes, dtype=float)
    w = np.array([1.0, 0.0])  # slope, intercept
    X = np.column_stack([x, np.ones_like(x)])
    for _ in range(iters):
        z = X @ w
        p = 1 / (1 + np.exp(-z))
        grad = X.T @ (y - p)
        hess = X.T @ (X * (p * (1 - p))[:, None])
        step = np.linalg.solve(hess, grad)
        w = w + step
        if np.abs(step).max() < 1e-10:
            break
    return float(w[0]), float(w[1])


@dataclass
class MoneylineConfig:
    """What the page and the grader need: the calibration and the validated bet threshold (in
    percentage points of win probability). Written once by the validation step; the final
    test's ROI at that threshold is added once it is scored."""

    calibration: Calibration
    threshold_pts: float
    chosen_on: str = ""
    final_test_roi: float | None = None  # both points pooled, at the closing price

    @property
    def flags_earned(self) -> bool:
        """Moneyline bets are flagged on the page only if the final test made money (M14);
        otherwise moneyline edges are shown as information only."""
        return self.final_test_roi is not None and self.final_test_roi > 0

    def save(self, path: Path) -> None:
        d = {"calibration": asdict(self.calibration), "threshold_pts": self.threshold_pts,
             "chosen_on": self.chosen_on, "final_test_roi": self.final_test_roi}  # fmt: skip
        path.write_text(json.dumps(d, indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> MoneylineConfig:
        d = json.loads(path.read_text())
        return cls(Calibration(**d["calibration"]), float(d["threshold_pts"]),
                   d.get("chosen_on", ""), d.get("final_test_roi"))  # fmt: skip
