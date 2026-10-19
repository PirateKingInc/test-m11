"""Ridge regression on standardised features (numpy), separate fits for margin and total,
and a normal-CDF win probability."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


@dataclass
class Ridge:
    names: tuple[str, ...]
    means: np.ndarray
    sds: np.ndarray
    coef: np.ndarray  # per standardised feature
    intercept: float
    lam: float

    def predict(self, X: np.ndarray) -> np.ndarray:
        return self.intercept + ((X - self.means) / self.sds) @ self.coef

    def predict_one(self, x: dict[str, float]) -> float:
        return float(self.predict(np.array([[x.get(n, 0.0) for n in self.names]]))[0])

    def contributions(self, x: dict[str, float]) -> dict[str, float]:
        """Each feature's push away from the training average, in points."""
        return {
            n: float(c * (x.get(n, 0.0) - m) / s)
            for n, m, s, c in zip(self.names, self.means, self.sds, self.coef, strict=True)
        }

    def raw_coef(self) -> dict[str, float]:
        return {n: float(c / s) for n, c, s in zip(self.names, self.coef, self.sds, strict=True)}

    def to_dict(self) -> dict:
        return dict(names=list(self.names), means=self.means.tolist(), sds=self.sds.tolist(),
                    coef=self.coef.tolist(), intercept=self.intercept, lam=self.lam)  # fmt: skip

    @classmethod
    def from_dict(cls, d: dict) -> Ridge:
        return cls(tuple(d["names"]), np.array(d["means"]), np.array(d["sds"]),
                   np.array(d["coef"]), d["intercept"], d["lam"])  # fmt: skip


def fit_ridge(X: np.ndarray, y: np.ndarray, names: Sequence[str], lam: float) -> Ridge:
    X = np.nan_to_num(np.asarray(X, dtype=float))
    means = X.mean(axis=0)
    sds = X.std(axis=0)
    sds[sds < 1e-9] = 1.0
    Z = (X - means) / sds
    yc = y - y.mean()
    k = Z.shape[1]
    coef = np.linalg.solve(Z.T @ Z + lam * np.eye(k), Z.T @ yc)
    return Ridge(tuple(names), means, sds, coef, float(y.mean()), lam)


def win_prob(margin: float, sigma: float) -> float:
    return 0.5 * (1 + math.erf(margin / (sigma * math.sqrt(2))))
