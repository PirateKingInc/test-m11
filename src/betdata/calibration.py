"""The parlay's calibrated engine probability (owner decision 2026-11-09, rule h): a Platt fit
(logistic regression of the outcome on the logit of the engine's raw probability) on every
graded engine prediction to date (ledger/engine_grades.csv), refit weekly on Mondays with only
the grades available then, and stored with its date (ledger/calibration_fits.csv, append-only).
The parlay's agreement bar (70% / 65%) uses the calibrated probability from the latest fit
dated on or before the day."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

FILE = "calibration_fits.csv"
FIELDS = ("fit_date", "n", "slope", "intercept", "fitted_at", "source")
MIN_N = 50
EPS = 1e-6


@dataclass(frozen=True)
class Fit:
    fit_date: str
    n: int
    slope: float
    intercept: float

    def __call__(self, p: float) -> float:
        p = min(max(p, EPS), 1 - EPS)
        z = self.slope * math.log(p / (1 - p)) + self.intercept
        return 1 / (1 + math.exp(-z))


def points(rows: list[dict]) -> tuple[list[float], list[int]]:
    """(engine probability, outcome) of every graded prediction (no push, no void)."""
    ps, ys = [], []
    for r in rows:
        if r.get("y") in ("0", "1") and r.get("p_engine") not in ("", None):
            ps.append(float(r["p_engine"]))
            ys.append(int(r["y"]))
    return ps, ys


def fit(ps: list[float], ys: list[int], iters: int = 100) -> tuple[float, float]:
    """Platt scaling by Newton's method on the logit: (slope, intercept)."""
    xs = [math.log(min(max(p, EPS), 1 - EPS) / (1 - min(max(p, EPS), 1 - EPS))) for p in ps]
    a, b = 1.0, 0.0
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for x, y in zip(xs, ys, strict=True):
            q = 1 / (1 + math.exp(-(a * x + b)))
            w = q * (1 - q)
            ga += (q - y) * x
            gb += q - y
            haa += w * x * x
            hab += w * x
            hbb += w
        det = haa * hbb - hab * hab + 1e-12
        da, db = (hbb * ga - hab * gb) / det, (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if abs(da) < 1e-9 and abs(db) < 1e-9:
            break
    return a, b


def maybe_refit(out: Path, now: datetime) -> Fit | None:
    """On a Monday (Eastern), or when no fit exists yet, fit once for the day from the grades
    in the ledger now; returns the new fit, if any."""
    from betdata.alerts.rules import EASTERN
    from betdata.ledger import append_rows, read_rows
    from betdata.nba.engine_grades import FILE as GRADES

    day = now.astimezone(EASTERN).date()
    have = read_rows(out / FILE)
    if any(r["fit_date"] == day.isoformat() for r in have):
        return None
    if have and day.weekday() != 0:  # Mondays only, once a first fit exists
        return None
    ps, ys = points(read_rows(out / GRADES))
    if len(ps) < MIN_N:
        return None
    a, b = fit(ps, ys)
    f = Fit(day.isoformat(), len(ps), round(a, 6), round(b, 6))
    append_rows(out / FILE, FIELDS, [{"fit_date": f.fit_date, "n": f.n, "slope": f.slope,
                                      "intercept": f.intercept, "fitted_at": now.isoformat(),
                                      "source": GRADES}], key="fit_date")  # fmt: skip
    return f


def current(out: Path, day: date) -> Fit | None:
    """The latest fit dated on or before `day`."""
    from betdata.ledger import read_rows

    rows = sorted((r for r in read_rows(out / FILE) if r["fit_date"] <= day.isoformat()),
                  key=lambda r: r["fit_date"])  # fmt: skip
    if not rows:
        return None
    r = rows[-1]
    return Fit(r["fit_date"], int(r["n"]), float(r["slope"]), float(r["intercept"]))
