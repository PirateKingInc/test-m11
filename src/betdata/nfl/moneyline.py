"""M13 moneyline for the M12 model: calibration on the validation seasons, the bet threshold,
and the single scoring of the test seasons.

The model's home win probability is Φ(margin / σ) from the frozen M12 fit (nfl/model.py). The
market is nflverse's closing moneyline with the vig removed proportionally. Both are scored with
the Brier score on the same games (ties dropped).

validate: fit the frozen design on 2013-2019, predict 2020-2022 at both points, and build a
  10-point reliability table and Brier scores for the model and the no-vig market. If the model
  is overconfident (logistic slope of the outcome on logit(p) below 1), one Platt calibration is
  fitted on those same validation predictions and kept; nothing else is tuned. The bet threshold
  (percentage points of win probability, model minus no-vig market) is the one with the best
  validation ROI among ML_THRESHOLDS with at least MIN_VALID_BETS bets, both points pooled, at
  the closing price. Written to config/nfl_moneyline.json.
final_test: refit on 2013-2022 (as the M12 final test did), apply that calibration and threshold
  to 2023-2025, once (guard file data/reports/m13_moneyline_final_test.json).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import Connection, select

from betdata.analysis.dataset import v_closing_lines
from betdata.config import PROJECT_ROOT
from betdata.core.matching import sport_id
from betdata.db.schema import games, markets
from betdata.moneyline import (
    Calibration,
    MoneylineConfig,
    fit_platt,
    no_vig,
    profit,
)
from betdata.nfl.pipeline import TEST, TRAIN, VALIDATE
from betdata.nfl.points import POINTS
from betdata.nfl.selection import Design, fit_design, predict_rows, wilson

ML_PATH = PROJECT_ROOT / "config" / "nfl_moneyline.json"
ML_THRESHOLDS = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)
MIN_VALID_BETS = 100


def closing_moneylines(conn: Connection, source: str = "nflverse") -> dict[int, tuple[int, int]]:
    """game_id -> (home price, away price) at the close."""
    sid = sport_id(conn, "nfl")
    out: dict[int, dict[str, int]] = {}
    for r in conn.execute(
        select(v_closing_lines.c.game_id, v_closing_lines.c.game_outcome,
               v_closing_lines.c.price_american)
        .join(markets, markets.c.market_id == v_closing_lines.c.market_id)
        .join(games, games.c.game_id == v_closing_lines.c.game_id)
        .where(games.c.sport_id == sid, v_closing_lines.c.source == source,
               markets.c.key == "h2h", markets.c.period == "game")
    ):  # fmt: skip
        if r.price_american is not None:
            out.setdefault(r.game_id, {})[r.game_outcome] = int(r.price_american)
    return {g: (d["home"], d["away"]) for g, d in out.items() if "home" in d and "away" in d}


def frame(pred: pd.DataFrame, out: pd.DataFrame, closes: dict) -> pd.DataFrame:
    """Predictions joined with the result and the no-vig closing probability (ties kept, with
    home_won NaN, so bets on them push)."""
    actual = out[["margin"]].rename(columns={"margin": "actual"})
    d = pred.merge(actual, left_on="game_id", right_index=True)
    d = d[d.game_id.isin(closes)].copy()
    prices = d.game_id.map(closes)
    d["home_price"] = [p[0] for p in prices]
    d["away_price"] = [p[1] for p in prices]
    d["market_p"] = [no_vig(h, a)[0] for h, a in zip(d.home_price, d.away_price, strict=True)]
    d["home_won"] = np.where(d.actual > 0, 1.0, np.where(d.actual < 0, 0.0, np.nan))
    return d.reset_index(drop=True)


def reliability(p, y, bins: int = 10) -> list[tuple]:
    p, y = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    edges = np.linspace(0, 1, bins + 1)
    table = []
    for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:], strict=True)):
        sel = (p >= lo) & ((p < hi) if i < bins - 1 else (p <= hi))
        if sel.any():
            table.append((round(lo, 1), round(hi, 1), int(sel.sum()), float(p[sel].mean()),
                          float(y[sel].mean())))  # fmt: skip
    return table


def brier(p, y) -> float:
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def scores(d: pd.DataFrame, calib: Calibration) -> dict:
    """Brier for the model (raw and calibrated) and the market on decided games."""
    s = d[d.home_won.notna()]
    y = s.home_won.to_numpy()
    cal = [calib(p) for p in s.win_prob]
    return {
        "games": int(len(s)),
        "brier_model": brier(s.win_prob, y),
        "brier_model_calibrated": brier(cal, y),
        "brier_market": brier(s.market_p, y),
        "bins_model": reliability(s.win_prob, y),
        "bins_model_calibrated": reliability(cal, y),
        "bins_market": reliability(s.market_p, y),
    }


def bets(d: pd.DataFrame, threshold_pts: float, calib: Calibration) -> list[dict]:
    """Back the side whose calibrated model probability beats the no-vig market by at least
    the threshold, at the closing price."""
    out = []
    for r in d.itertuples(index=False):
        p = calib(r.win_prob)
        e = 100 * (p - r.market_p)
        if abs(e) < threshold_pts:
            continue
        side = "home" if e > 0 else "away"
        price = r.home_price if side == "home" else r.away_price
        if np.isnan(r.home_won):
            result = "push"
        else:
            result = "win" if (r.home_won == 1.0) == (side == "home") else "loss"
        out.append(dict(game_id=int(r.game_id), point=r.point, side=side, price=int(price),
                        edge=round(abs(e), 2), result=result,
                        profit=profit(result, price)))  # fmt: skip
    return out


def summary(bl: list[dict]) -> dict:
    w = sum(b["result"] == "win" for b in bl)
    lo = sum(b["result"] == "loss" for b in bl)
    pu = sum(b["result"] == "push" for b in bl)
    n = w + lo + pu
    prof = sum(b["profit"] for b in bl)
    return dict(record=f"{w}-{lo}-{pu}", bets=n, win_rate=w / max(w + lo, 1),
                ci95=wilson(w, w + lo), profit=round(prof, 3),
                roi=prof / n if n else None,
                avg_price=float(np.mean([b["price"] for b in bl])) if bl else None)  # fmt: skip


def choose_threshold(d: pd.DataFrame, calib: Calibration) -> tuple[float, dict]:
    by = {t: summary(bets(d, t, calib)) for t in ML_THRESHOLDS}
    eligible = [t for t, s in by.items() if s["bets"] >= MIN_VALID_BETS]
    if not eligible:
        return min(ML_THRESHOLDS), by
    return max(eligible, key=lambda t: (by[t]["roi"], -t)), by


def validate(rows, out, design: Design, closes: dict, train=TRAIN,
             valid=VALIDATE) -> tuple[dict, MoneylineConfig]:  # fmt: skip
    fits = fit_design(rows, out, design, train)
    pred = predict_rows(rows[rows.season.isin(valid)], fits)
    d = frame(pred, out, closes)
    decided = d[d.home_won.notna()]
    slope, intercept = fit_platt(decided.win_prob, decided.home_won)
    overconfident = slope < 1.0
    calib = Calibration(slope, intercept, fitted_on="2020-2022 validation, both points",
                        applied=overconfident)  # fmt: skip
    threshold, by = choose_threshold(d, calib)
    report = {
        "fit_seasons": list(train), "seasons": list(valid),
        "platt": {"slope": slope, "intercept": intercept, "overconfident": overconfident},
        "points": {p: scores(d[d.point == p], calib) for p in POINTS},
        "thresholds": {str(t): s for t, s in by.items()},
        "threshold_pts": threshold,
    }  # fmt: skip
    return report, MoneylineConfig(calib, threshold, "validation 2020-2022, both points pooled")


def final_test(rows, out, design: Design, closes: dict, cfg: MoneylineConfig,
               report_path: Path, force: bool = False, fit_seasons=(*TRAIN, *VALIDATE),
               test_seasons=TEST) -> dict:  # fmt: skip
    if report_path.exists() and not force:
        raise RuntimeError(f"test seasons already scored ({report_path}); they are scored once")
    fits = fit_design(rows, out, design, fit_seasons)
    pred = predict_rows(rows[rows.season.isin(test_seasons)], fits)
    d = frame(pred, out, closes)
    report = {
        "fit_seasons": list(fit_seasons), "seasons": list(test_seasons),
        "calibration": cfg.calibration.__dict__, "threshold_pts": cfg.threshold_pts,
        "points": {},
    }  # fmt: skip
    for p in POINTS:
        dp = d[d.point == p]
        report["points"][p] = {**scores(dp, cfg.calibration),
                               "bets": summary(bets(dp, cfg.threshold_pts, cfg.calibration))}
    report["bets_pooled"] = summary(bets(d, cfg.threshold_pts, cfg.calibration))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=1, default=float))
    return report
