"""F's calibration (owner decision 2026-11-12, MODEL.md "F calibration"): one global Platt fit
(logistic regression of the result on logit(fair_prob)) on every graded F pick (win/loss), one
pick per player per game (the highest model probability), refitted every Monday in the same job
as the parlay's calibration refit (`ledger.sync`), and the first time when none exists.

- **Guard:** a fit is used only if it is increasing (slope > 0) and n >= 200; otherwise the
  previous fit stays in force; the attempt is recorded with its reason (status `rejected`).
- **File:** `ledger/f_calibration_fits.csv` (model F, fit date, n, slope, intercept, status,
  reason), append-only. Not `calibration_fits.csv`: the parlay reads that file's latest row
  whatever its source and keys it by date, so an F row there would change the parlay.
- **Stored:** each new F pick (created after the fit it uses) gets a write-once `f_cal`
  annotation: its calibrated probability and the fit date.
- **Ranking:** OG Ranking v2 ranks on `calibrate(fair_prob)`. An increasing fit keeps every pick
  and its order (MODEL.md; tests/test_f_calibration.py)."""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

from betdata.calibration import Fit, fit

log = logging.getLogger(__name__)
FILE = "f_calibration_fits.csv"
FIELDS = ("model", "fit_date", "n", "slope", "intercept", "fitted_at", "status", "reason")
MODEL = "F"
MIN_N = 200
SIGNAL_NAME = "F"


def points(out: Path, before: datetime | None = None) -> tuple[list[float], list[int]]:
    """(model probability, win) of every graded F pick (win/loss), one per player per game;
    `before`: only picks graded before then (walk-forward)."""
    from betdata.f_verdict import dedup
    from betdata.ledger import counted, joined

    rows = [r for r in counted(joined(out))
            if r["signal_name"] == SIGNAL_NAME and r.get("result") in ("win", "loss")
            and r.get("fair_prob")
            and (before is None or (r.get("graded_at")
                                    and datetime.fromisoformat(r["graded_at"]) < before))]
    rows = dedup(rows)
    return [float(r["fair_prob"]) for r in rows], [int(r["result"] == "win") for r in rows]


def fit_points(ps: list[float], ys: list[int], day: date) -> tuple[Fit | None, str]:
    """(fit, "") if acceptable, else (None, why)."""
    if len(ps) < MIN_N:
        return None, f"n {len(ps)} < {MIN_N}"
    a, b = fit(ps, ys)
    if not a > 0:
        return None, f"slope {a:.4f} <= 0 (not increasing)"
    return Fit(day.isoformat(), len(ps), round(a, 6), round(b, 6)), ""


def maybe_refit(out: Path, now: datetime) -> Fit | None:
    """On a Monday (Eastern), or when no accepted fit exists yet: fit once for the day. Returns
    the new fit if accepted (a rejected attempt is recorded and the previous fit stays)."""
    from betdata.alerts.rules import EASTERN
    from betdata.ledger import append_rows, read_rows

    day = now.astimezone(EASTERN).date()
    have = read_rows(out / FILE)
    if any(r["fit_date"] == day.isoformat() for r in have):
        return None
    if any(r["status"] == "ok" for r in have) and day.weekday() != 0:
        return None
    ps, ys = points(out)
    f, why = fit_points(ps, ys, day)
    row = {"model": MODEL, "fit_date": day.isoformat(), "n": len(ps),
           "fitted_at": now.isoformat(), "status": "ok" if f else "rejected", "reason": why}
    if f is not None:
        row.update(slope=f.slope, intercept=f.intercept)
    else:
        log.warning("F calibration fit %s rejected (%s); the previous fit stays", day, why)
    append_rows(out / FILE, FIELDS, [row], key="fit_date")
    return f


def current(out: Path, day: date | None = None) -> Fit | None:
    """The latest accepted fit dated on or before `day` (None: the latest)."""
    from betdata.ledger import read_rows

    rows = sorted((r for r in read_rows(out / FILE) if r["status"] == "ok"
                   and (day is None or r["fit_date"] <= day.isoformat())),
                  key=lambda r: r["fit_date"])  # fmt: skip
    if not rows:
        return None
    r = rows[-1]
    return Fit(r["fit_date"], int(r["n"]), float(r["slope"]), float(r["intercept"]))


def calibrate(p: float, f: Fit | None) -> float:
    """The calibrated probability (the raw one when no fit exists yet)."""
    return float(p) if f is None else f(float(p))


def annotate(out: Path, now: datetime) -> int:
    """A write-once `f_cal` annotation on each F pick created after the fit in force then (the
    latest accepted fit fitted before the pick): its calibrated probability and the fit date."""
    from betdata.ledger import ANNOTATION_FIELDS, _iso, append_rows, read_rows

    fits = sorted((r for r in read_rows(out / FILE) if r["status"] == "ok"),
                  key=lambda r: r["fitted_at"])  # fmt: skip
    if not fits:
        return 0
    have = {r["key"] for r in read_rows(out / "annotations.csv")}
    rows = []
    for pk in read_rows(out / "picks.csv"):
        if pk["signal_name"] != SIGNAL_NAME or not pk.get("fair_prob"):
            continue
        key = f"{pk['pick_id']}:f_cal"
        if key in have:
            continue
        t = datetime.fromisoformat(pk["created_at"])
        use = [r for r in fits if datetime.fromisoformat(r["fitted_at"]) <= t]
        if not use:
            continue  # created before any fit: an old pick, never rewritten
        r = use[-1]
        f = Fit(r["fit_date"], int(r["n"]), float(r["slope"]), float(r["intercept"]))
        rows.append({"key": key, "pick_id": pk["pick_id"], "field": "f_cal",
                     "value": f"{calibrate(float(pk['fair_prob']), f):.4f}",
                     "noted_at": _iso(now),
                     "note": f"F calibration fit {f.fit_date} (slope {f.slope}, intercept "
                             f"{f.intercept}, n {f.n}); owner 2026-11-12"})  # fmt: skip
    return append_rows(out / "annotations.csv", ANNOTATION_FIELDS, rows, key="key")
