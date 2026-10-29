"""Prop distributions' medians and quantiles (M18 Part D, MODEL.md "M18 Part D").

Every prop model stores, beside its mean, the median and the 10/25/50/75/90% quantiles of the
distribution its over probabilities come from:
- nbap15 and NFL receptions: a negative binomial with variance a + b*mean + c*mean^2;
- NFL yards: that negative binomial, or a normal floored at 0;
- anytime TD: a Poisson.

The median-anchored variant solves for the negative binomial mean whose median equals a
median projection (`mean_for_median`).
"""

from __future__ import annotations

import numpy as np
from scipy.stats import nbinom, norm, poisson

QS = {"q10": 0.10, "q25": 0.25, "median": 0.50, "q75": 0.75, "q90": 0.90}


def nb_params(disp, mu):
    mu = np.maximum(np.asarray(mu, dtype=float), 0.05)
    v = disp.var(mu)
    n = mu * mu / (v - mu)
    return n, n / (n + mu)


def quantiles(dist, mu: float) -> dict[str, float]:
    """{"q10", "q25", "median", "q75", "q90"} of the distribution at mean `mu`. `dist`: a
    Dispersion (negative binomial), an NFL YardsDist, or None (Poisson)."""
    family = getattr(dist, "family", "negbin")
    if dist is None:
        vals = {k: poisson.ppf(q, max(mu, 0.01)) for k, q in QS.items()}
    elif family == "normal":
        sd = float(dist.sd(mu))
        vals = {k: max(0.0, norm.ppf(q, loc=mu, scale=sd)) for k, q in QS.items()}
    else:
        disp = getattr(dist, "disp", dist)
        n, p = nb_params(disp, mu)
        vals = {k: nbinom.ppf(q, float(n), float(p)) for k, q in QS.items()}
    return {k: round(float(v), 2) for k, v in vals.items()}


def smooth_median(disp, mu) -> np.ndarray:
    """The continuous median of the negative binomial: the point where the CDF, linearly
    interpolated between whole numbers (X counted at x + 0.5), crosses 1/2."""
    n, p = nb_params(disp, mu)
    k = nbinom.ppf(0.5, n, p)  # smallest whole k with F(k) >= 0.5
    f_lo = nbinom.cdf(k - 1, n, p)
    f_hi = nbinom.cdf(k, n, p)
    frac = np.where(f_hi > f_lo, (0.5 - f_lo) / np.maximum(f_hi - f_lo, 1e-12), 0.5)
    return k - 0.5 + frac


def mean_for_median(disp, target, lo: float = 0.05, hi: float | None = None,
                    iters: int = 60) -> np.ndarray:  # fmt: skip
    """The negative binomial mean (frozen dispersion) whose smooth median equals `target`
    (bisection; the median increases with the mean)."""
    target = np.maximum(np.asarray(target, dtype=float), 0.0)
    a = np.full(target.shape, lo)
    b = np.full(target.shape, hi if hi is not None else 1.0) * (2 * target + 10)
    for _ in range(iters):
        m = (a + b) / 2
        below = smooth_median(disp, m) < target
        a = np.where(below, m, a)
        b = np.where(below, b, m)
    return (a + b) / 2
