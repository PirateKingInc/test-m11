"""M14 model selection on validation seasons, calibration, betting thresholds, the final test
(scored once) and the frozen live fit (MODEL.md, M14). Driven by `betdata nba ...`.

Historical lines (ESPN): 2015-16 has an opening line (the "Opening" book) and every season the
last (closing) line; 2023-24 on also has each book's opening line. 2017-18 to 2022-23 have
closing lines only, so the validation seasons have no line known at a prediction point.
Validation bets are therefore graded at the closing line and price (the M12/M13 backtest
convention); the final test is graded both at the opening line (the prediction-time market) and
at the close. Closing lines are never model inputs.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from betdata.config import PROJECT_ROOT
from betdata.moneyline import fair_american, no_vig
from betdata.nba.pipeline import (
    LAMBDAS,
    TEST,
    TRAIN,
    VALIDATE,
    columns,
    evaluate,
    fit_point,
)
from betdata.nba.points import POINTS

log = logging.getLogger(__name__)
SPREAD_THRESHOLDS = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
ML_THRESHOLDS = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0)
MIN_VALID_BETS = 100
FITS_DIR = PROJECT_ROOT / "config" / "fits"
DESIGN_PATH = PROJECT_ROOT / "config" / "nba_model.json"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def elo_mae(rows: pd.DataFrame, out: pd.DataFrame, seasons) -> dict[str, float]:
    """The Elo baseline: Elo difference plus home court, as a margin (no fitting)."""
    r = rows[rows.season.isin(seasons)].join(out, on="game_id", how="inner")
    pred = r.m_base_elo + r.m_base_home * 100 / 28
    return {p: float(np.abs(pred[r.point == p] - r.margin[r.point == p]).mean()) for p in POINTS}


def market_frame(store, game_ids) -> pd.DataFrame:
    """Per game: the opening line and price (the backtest's prediction-time market) and the
    closing line and price."""
    o = store.frame("odds").df
    opening = o[o.kind == "open"].groupby("game_id").last()
    close = store.params["closing"]
    df = pd.DataFrame(index=pd.Index(list(game_ids), name="game_id"))
    for col in ("spread_home", "ml_home", "ml_away", "total"):
        df[f"open_{col}"] = opening[col] if col in opening else np.nan
        df[f"close_{col}"] = close[col] if col in close else np.nan
    return df


def predictions(rows, out, fits: dict, seasons) -> pd.DataFrame:
    parts = []
    for p, f in fits.items():
        r = rows[(rows.point == p) & rows.season.isin(seasons)].join(out, on="game_id",
                                                                       how="inner")
        Xm = np.nan_to_num(r[list(f.margin.names)].to_numpy(dtype=float))
        Xt = np.nan_to_num(r[list(f.total.names)].to_numpy(dtype=float))
        d = r[["game_id", "point", "season", "margin", "total"]].copy()
        d["pred"] = f.margin.predict(Xm)
        d["pred_total"] = f.total.predict(Xt)
        d["sigma"] = f.sigma
        parts.append(d)
    return pd.concat(parts, ignore_index=True)


def win_probs(pred: pd.DataFrame, calib: tuple[float, float] | None = None) -> np.ndarray:
    z = pred.pred / pred.sigma
    p = 0.5 * (1 + np.vectorize(math.erf)(z / math.sqrt(2)))
    if calib:
        a, b = calib
        lp = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
        p = 1 / (1 + np.exp(-(a * lp + b)))
    return p


def platt(p: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    from betdata.moneyline import fit_platt

    return fit_platt(p, y)


def spread_bets(pred: pd.DataFrame, mkt: pd.DataFrame, threshold: float,
                at: str = "open") -> pd.DataFrame:  # fmt: skip
    d = pred.join(mkt, on="game_id")
    line = f"{at}_spread_home"
    d = d[d[line].notna()]
    edge = d.pred + d[line]
    d = d[edge.abs() >= threshold].copy()
    side = np.sign(edge[d.index])
    cover = side * (d.margin + d[line])
    d["result"] = np.where(cover > 0, "win", np.where(cover < 0, "loss", "push"))
    d["profit"] = np.where(d.result == "win", 100 / 110, np.where(d.result == "loss", -1.0, 0))
    d["clv"] = np.where(d.close_spread_home.notna(),
                        side * (d[line] - d.close_spread_home), np.nan)
    return d


def ml_bets(pred: pd.DataFrame, mkt: pd.DataFrame, threshold: float, probs,
            at: str = "open") -> pd.DataFrame:  # fmt: skip
    d = pred.join(mkt, on="game_id").copy()
    d["p"] = probs
    hcol, acol = f"{at}_ml_home", f"{at}_ml_away"
    d = d[d[hcol].notna() & d[acol].notna()]
    nv = np.array([no_vig(int(h), int(a))[0] if no_vig(int(h), int(a)) else np.nan
                   for h, a in zip(d[hcol], d[acol], strict=True)])  # fmt: skip
    edge = 100 * (d.p - nv)
    keep = np.abs(edge) >= threshold
    d = d[keep].copy()
    home = edge[keep] > 0
    price = np.where(home, d[hcol], d[acol])
    won = np.where(home, d.margin > 0, d.margin < 0)
    d["result"] = np.where(d.margin == 0, "push", np.where(won, "win", "loss"))
    d["profit"] = np.where(d.result == "win", np.where(price > 0, price / 100, 100 / -price),
                           np.where(d.result == "loss", -1.0, 0.0))
    return d


def record(b: pd.DataFrame) -> dict:
    w, lo, pu = int((b.result == "win").sum()), int((b.result == "loss").sum()), int(
        (b.result == "push").sum())
    n = w + lo + pu
    ci = wilson(w, w + lo)
    return {"bets": n, "w": w, "l": lo, "p": pu,
            "win_rate": round(w / (w + lo), 4) if w + lo else None,
            "win_rate_95": [round(ci[0], 4), round(ci[1], 4)],
            "roi": round(float(b.profit.sum()) / n, 4) if n else None,
            "clv": round(float(b.clv.mean()), 3) if "clv" in b and b.clv.notna().any()
            else None}  # fmt: skip


def choose_threshold(fn, thresholds: Sequence[float]) -> tuple[float, dict]:
    by = {t: record(fn(t)) for t in thresholds}
    ok = [t for t in thresholds if by[t]["bets"] >= MIN_VALID_BETS]
    if not ok:
        return min(thresholds), by
    return max(ok, key=lambda t: (by[t]["roi"], -t)), by


def blend_weight(pred: pd.DataFrame, mkt: pd.DataFrame) -> tuple[float, dict]:
    d = pred.join(mkt, on="game_id")
    d = d[d.open_spread_home.notna()]
    by = {}
    for w in np.round(np.arange(0, 1.01, 0.1), 2):
        by[float(w)] = float(np.abs(w * d.pred + (1 - w) * -d.open_spread_home - d.margin).mean())
    return min(by, key=by.get), by


def fits_for(rows, out, m_cols, t_cols, train, lam_m, lam_t) -> dict:
    return {p: fit_point(rows, out, p, train, m_cols, t_cols, lam_m, lam_t) for p in POINTS}


def save_fit(fits: dict, play: dict, seasons, design: dict) -> Path:
    raw = {"seasons": list(seasons), "design": design, "play_prob": play,
           "fits": {p: {"margin": f.margin.to_dict(), "total": f.total.to_dict(),
                        "sigma": f.sigma} for p, f in fits.items()}}  # fmt: skip
    path = FITS_DIR / f"nba_fit_{seasons[0]}_{seasons[-1]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, indent=1, sort_keys=True) + "\n")
    return path


__all__ = ["LAMBDAS", "TEST", "TRAIN", "VALIDATE", "columns", "evaluate", "fair_american"]
