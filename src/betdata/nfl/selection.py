"""Model selection (ridge vs gradient boosting), win-probability calibration, ablation, and the
single scoring of the test seasons.

The kept design (which groups' margin and total features, and the ridge penalties) is written
by `betdata nfl evaluate --write-design` to config/nfl_model.json and read by everything here
and by live predictions, so nothing is re-chosen after the test seasons are seen.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from betdata.config import PROJECT_ROOT
from betdata.nfl.model import win_prob
from betdata.nfl.pipeline import Fit, _xy, fit_point
from betdata.nfl.points import POINTS

DESIGN_PATH = PROJECT_ROOT / "config" / "nfl_model.json"
# Bet when the model and the line differ by at least this many points: the existing
# thresholds (3 on spreads, chosen for Elo in M10; 4 on totals, chosen for M11).
BET_THRESHOLDS = {"spread": 3.0, "total": 4.0}


@dataclass
class Design:
    margin_groups: list[str]
    total_groups: list[str]
    lam_margin: float
    lam_total: float
    model: str = "ridge"  # or "gbm" if it wins clearly on validation
    # Model weight in w * model + (1 - w) * market, chosen on validation seasons.
    blend_margin: float = 0.5
    blend_total: float = 0.5

    def columns(self, rows_columns) -> tuple[list[str], list[str]]:
        from betdata.nfl.groups import columns

        m = [c for g in self.margin_groups for c in columns(rows_columns, g)[0]]
        t = [c for g in self.total_groups for c in columns(rows_columns, g)[1]]
        return m, t

    @property
    def groups(self) -> list[str]:
        out = []
        for g in [*self.margin_groups, *self.total_groups]:
            if g not in out:
                out.append(g)
        return out

    def save(self, path: Path = DESIGN_PATH) -> None:
        path.write_text(json.dumps(self.__dict__, indent=2) + "\n")

    @classmethod
    def load(cls, path: Path = DESIGN_PATH) -> Design:
        return cls(**json.loads(path.read_text()))


def predict_rows(rows: pd.DataFrame, fits: dict[str, Fit]) -> pd.DataFrame:
    """Margin, total and win probability for every row, with the fit of its point."""
    out = []
    for p, f in fits.items():
        r = rows[rows.point == p]
        Xm = np.nan_to_num(r[list(f.margin.names)].to_numpy(dtype=float))
        Xt = np.nan_to_num(r[list(f.total.names)].to_numpy(dtype=float))
        pm, pt = f.margin.predict(Xm), f.total.predict(Xt)
        out.append(pd.DataFrame({"game_id": r.game_id.to_numpy(), "point": p, "margin": pm,
                                 "total": pt,
                                 "win_prob": [win_prob(m, f.sigma) for m in pm]}))  # fmt: skip
    return pd.concat(out, ignore_index=True)


def fit_design(rows, out, design: Design, seasons: Sequence[int]) -> dict[str, Fit]:
    m, t = design.columns(rows.columns)
    return {p: fit_point(rows, out, p, seasons, m, t, design.lam_margin, design.lam_total)
            for p in POINTS}  # fmt: skip


# --- ridge vs gradient boosting ---------------------------------------------------------------


def gbm_mae(rows, out, design: Design, train, valid, seed: int = 0) -> dict[str, tuple]:
    """Validation MAE of gradient boosting on the same features (absolute-error loss)."""
    from sklearn.ensemble import HistGradientBoostingRegressor

    m_cols, t_cols = design.columns(rows.columns)
    res = {}
    for p in POINTS:
        tr = rows[(rows.point == p) & rows.season.isin(train)]
        va = rows[(rows.point == p) & rows.season.isin(valid)]
        errs = []
        for cols, target in ((m_cols, "margin"), (t_cols, "total")):
            Xtr, ytr, _ = _xy(tr, out, cols, target)
            Xva, yva, _ = _xy(va, out, cols, target)
            gbm = HistGradientBoostingRegressor(loss="absolute_error", max_iter=300,
                                                learning_rate=0.03, max_leaf_nodes=15,
                                                min_samples_leaf=40, l2_regularization=1.0,
                                                random_state=seed)  # fmt: skip
            gbm.fit(Xtr, ytr)
            errs.append(float(np.abs(gbm.predict(Xva) - yva).mean()))
        res[p] = tuple(errs)
    return res


# --- calibration --------------------------------------------------------------------------------


def calibration(pred: pd.DataFrame, out: pd.DataFrame, bins: int = 10) -> dict:
    """Brier score, log loss and a reliability table for the home win probability (ties
    dropped)."""
    d = pred.merge(out[["margin"]].rename(columns={"margin": "actual"}), left_on="game_id",
                   right_index=True)  # fmt: skip
    d = d[d.actual != 0]
    y = (d.actual > 0).astype(float).to_numpy()
    p = d.win_prob.clip(1e-6, 1 - 1e-6).to_numpy()
    edges = np.linspace(0, 1, bins + 1)
    table = []
    for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:], strict=True)):
        sel = (p >= lo) & ((p < hi) if i < bins - 1 else (p <= hi))
        if sel.any():
            table.append((round(lo, 1), round(hi, 1), int(sel.sum()), float(p[sel].mean()),
                          float(y[sel].mean())))  # fmt: skip
    return {
        "brier": float(((p - y) ** 2).mean()),
        "logloss": float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean()),
        "n": int(len(y)),
        "bins": table,
    }


def mae_of(pred: pd.DataFrame, out: pd.DataFrame) -> tuple[float, float, int]:
    d = pred.merge(out.rename(columns={"margin": "am", "total": "at"}), left_on="game_id",
                   right_index=True)  # fmt: skip
    return float((d.margin - d.am).abs().mean()), float((d.total - d["at"]).abs().mean()), len(d)


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    phat = wins / n
    c = (phat + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return c - h, c + h
