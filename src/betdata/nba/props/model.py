"""NBA prop model (MODEL.md, M15): projected minutes, per-minute rates with teammates' shares
redistributed, game environment and opponent, and a negative binomial per stat."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import nbinom, poisson

from betdata.nba.props.panel import MARKETS
from betdata.nfl.model import Ridge, fit_ridge

MIN_FEATURES = ("min_l3", "min_l10", "min_l30", "min_ewm", "start_l10", "b2b", "rest",
                "since_return_c", "ret_flag", "pf_rate", "abs_margin", "abs_margin_start",
                "mis_min", "mis_start_min", "own_q", "own_prob", "new_team", "n_team_c",
                "early")  # fmt: skip
STAT_TERMS = ("m_r10", "m_r30", "redis", "opp", "env", "g10")
LEAGUE_TOTAL = 220.0  # the side model's total is read relative to this (training mean, M14)


def prepare(f: pd.DataFrame) -> pd.DataFrame:
    """Derived minutes inputs (clipped counters and interactions)."""
    f = f.copy()
    f["since_return_c"] = np.minimum(f.since_return.fillna(99), 10)
    f["ret_flag"] = (f.since_return_c < 3).astype(float)
    f["abs_margin"] = f.abs_margin.fillna(f.abs_margin.median() if f.abs_margin.notna().any()
                                          else 6.0)  # fmt: skip
    f["abs_margin_start"] = f.abs_margin * f.start_l10
    f["n_team_c"] = np.minimum(f.n_team.fillna(10), 10)
    f["early"] = (f.n_season.fillna(0) < 5).astype(float)
    return f


def stat_terms(f: pd.DataFrame, minutes: np.ndarray, market: str) -> pd.DataFrame:
    s = MARKETS[market]
    r30 = f[f"r30_{s}"].fillna(0.0).to_numpy()
    mis = np.clip(f[f"mis_{s}"].fillna(0.0).to_numpy(), 0, 0.8)
    base = minutes * r30
    total = f.side_total.fillna(LEAGUE_TOTAL).to_numpy()
    return pd.DataFrame({
        "m_r10": minutes * f[f"r10_{s}"].fillna(0.0).to_numpy(),
        "m_r30": base,
        "redis": base * mis / (1 - mis),
        "opp": base * (f[f"opp_{s}"].fillna(1.0).to_numpy() - 1),
        "env": base * (total / LEAGUE_TOTAL - 1),
        "g10": f[f"g10_{s}"].fillna(0.0).to_numpy(),
    }, index=f.index)  # fmt: skip


@dataclass
class Dispersion:
    """Variance as a + b*mean + c*mean^2 (fitted on training residuals), as a negative binomial
    (Poisson where the variance is not above the mean)."""

    a: float
    b: float
    c: float

    def var(self, mu: np.ndarray) -> np.ndarray:
        return np.maximum(self.a + self.b * mu + self.c * mu * mu, mu * 1.0001 + 1e-6)

    def p_over(self, mu: np.ndarray, line: np.ndarray) -> np.ndarray:
        """P(X > line); for a whole-number line, a push (X == line) is neither."""
        mu = np.maximum(np.asarray(mu, dtype=float), 0.05)
        v = self.var(mu)
        n = mu * mu / (v - mu)
        p = n / (n + mu)
        k = np.floor(np.asarray(line, dtype=float))
        return 1 - nbinom.cdf(k, n, p)

    def p_push(self, mu: np.ndarray, line: np.ndarray) -> np.ndarray:
        mu = np.maximum(np.asarray(mu, dtype=float), 0.05)
        line = np.asarray(line, dtype=float)
        v = self.var(mu)
        n = mu * mu / (v - mu)
        p = n / (n + mu)
        whole = np.isclose(line, np.round(line))
        return np.where(whole, nbinom.pmf(np.round(line), n, p), 0.0)

    def to_dict(self) -> dict:
        return {"family": "negbin", "a": self.a, "b": self.b, "c": self.c}


def fit_dispersion(mu: np.ndarray, y: np.ndarray, bins: int = 20) -> Dispersion:
    """Least squares of squared residuals on (1, mu, mu^2) over mean bins."""
    order = np.argsort(mu)
    chunks = np.array_split(order, bins)
    m = np.array([mu[c].mean() for c in chunks])
    v = np.array([np.mean((y[c] - mu[c]) ** 2) for c in chunks])
    A = np.vstack([np.ones_like(m), m, m * m]).T
    a, b, c = np.linalg.lstsq(A, v, rcond=None)[0]
    return Dispersion(float(a), float(b), float(max(c, 0.0)))


@dataclass
class PropModel:
    minutes: Ridge
    stats: dict[str, Ridge]
    disp: dict[str, Dispersion]
    calib: dict[str, tuple[float, float]] = field(default_factory=dict)

    def predict_minutes(self, f: pd.DataFrame) -> np.ndarray:
        X = f[list(self.minutes.names)].fillna(0.0).to_numpy(dtype=float)
        return np.clip(self.minutes.predict(X), 0, 48)

    def predict(self, f: pd.DataFrame, market: str, minutes: np.ndarray | None = None,
                ) -> np.ndarray:  # fmt: skip
        m = self.predict_minutes(f) if minutes is None else minutes
        X = stat_terms(f, m, market)[list(self.stats[market].names)].to_numpy(dtype=float)
        return np.maximum(self.stats[market].predict(X), 0.0)

    def p_over(self, market: str, mu, line) -> np.ndarray:
        p = self.disp[market].p_over(mu, line)
        if market in self.calib:
            a, b = self.calib[market]
            lp = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
            p = 1 / (1 + np.exp(-(a * lp + b)))
        return p

    def to_dict(self) -> dict:
        return {"minutes": self.minutes.to_dict(),
                "stats": {k: v.to_dict() for k, v in self.stats.items()},
                "disp": {k: v.to_dict() for k, v in self.disp.items()},
                "calib": {k: list(v) for k, v in self.calib.items()}}  # fmt: skip

    @classmethod
    def from_dict(cls, d: dict) -> PropModel:
        return cls(Ridge.from_dict(d["minutes"]),
                   {k: Ridge.from_dict(v) for k, v in d["stats"].items()},
                   {k: Dispersion(v["a"], v["b"], v["c"]) for k, v in d["disp"].items()},
                   {k: tuple(v) for k, v in d.get("calib", {}).items()})  # fmt: skip


def fit(train: pd.DataFrame, min_features=MIN_FEATURES, terms=STAT_TERMS, lam_min=10.0,
        lam_stat=10.0) -> PropModel:  # fmt: skip
    """`train`: prepared features of played player-games with the actual stats."""
    Xm = train[list(min_features)].fillna(0.0).to_numpy(dtype=float)
    mins = fit_ridge(Xm, train.minutes.to_numpy(dtype=float), min_features, lam_min)
    m_hat = np.clip(mins.predict(Xm), 0, 48)
    stats, disp = {}, {}
    for market, s in MARKETS.items():
        X = stat_terms(train, m_hat, market)[list(terms)].to_numpy(dtype=float)
        y = train[s].to_numpy(dtype=float)
        r = fit_ridge(X, y, terms, lam_stat)
        stats[market] = r
        disp[market] = fit_dispersion(np.maximum(r.predict(X), 0.05), y)
    return PropModel(mins, stats, disp)


def poisson_p_over(mu, line) -> np.ndarray:
    return 1 - poisson.cdf(np.floor(np.asarray(line, dtype=float)), np.maximum(mu, 0.05))
