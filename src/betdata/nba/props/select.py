"""NBA prop model selection on validation (MODEL.md, M15): error vs the last-10 average per
market, calibration of over probabilities, and the market backtest once lines are loaded."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from betdata.nba.pipeline import TEST, TRAIN, VALIDATE
from betdata.nba.props import model as M
from betdata.nba.props.panel import MARKETS

log = logging.getLogger(__name__)
FEATURES_CACHE = Path("data/derived/nba_prop_features.pkl")
REPORTS = Path("reports/nba")
BINS = (0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0)


def history_features(rebuild: bool = False) -> pd.DataFrame:
    """Features and actuals of every played player-game 2015-16 to 2025-26 at both points
    (cached under data/derived; built from the database and the M14 feature rows)."""
    if FEATURES_CACHE.exists() and not rebuild:
        return pd.read_pickle(FEATURES_CACHE)
    from betdata.nba.cli import _engine, _rows
    from betdata.nba.data import outcomes
    from betdata.nba.live import Design, load_fit
    from betdata.nba.props.panel import Panel
    from betdata.nba.props.side import side_history

    store, out, rows, _ = _rows()
    _, engine = _engine()
    with engine.connect() as conn:
        panel = Panel.load(conn)
    side = side_history(rows, outcomes(store))
    play = load_fit(Design.load())[1]
    tg = panel.played_targets(seasons=range(2015, 2026))
    f = panel.features(tg, side, play)
    y = panel.p.set_index(["game_id", "player_id"])[["minutes", "pts", "reb", "ast", "fg3m",
                                                     "pra"]]  # fmt: skip
    f = f.join(y, on=["game_id", "player_id"])
    FEATURES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    f.to_pickle(FEATURES_CACHE)
    return f


def usable(f: pd.DataFrame) -> pd.DataFrame:
    return M.prepare(f[f.has_state & f.min_l10.notna()])


def calibration(p: np.ndarray, y: np.ndarray) -> list[dict]:
    b = pd.cut(p, list(BINS))
    t = pd.DataFrame({"p": p, "y": y, "b": b}).groupby("b", observed=True).agg(
        n=("y", "size"), pred=("p", "mean"), actual=("y", "mean"))  # fmt: skip
    return [{"bin": str(i), "n": int(r.n), "pred": round(float(r.pred), 3),
             "actual": round(float(r.actual), 3)} for i, r in t.iterrows()]  # fmt: skip


def synthetic_line(f: pd.DataFrame, stat: str) -> np.ndarray:
    """A half-point line just under the last-10 average (for calibration where no market line
    exists)."""
    return np.floor(f[f"g10_{stat}"].to_numpy()) + 0.5


def score(mod: M.PropModel, va: pd.DataFrame) -> dict:
    m_hat = mod.predict_minutes(va)
    out = {"minutes": {"mae": float(np.abs(m_hat - va.minutes).mean()),
                       "mae_last10": float(np.abs(va.min_l10 - va.minutes).mean())}}  # fmt: skip
    for mk, s in MARKETS.items():
        mu = mod.predict(va, mk, m_hat)
        y = va[s].to_numpy()
        base = va[f"g10_{s}"].to_numpy()
        line = synthetic_line(va, s)
        p = mod.p_over(mk, mu, line)
        hit = (y > line).astype(float)
        out[mk] = {"n": int(len(y)), "mae": float(np.abs(mu - y).mean()),
                   "mae_last10": float(np.abs(base - y).mean()),
                   "rmse": float(np.sqrt(np.mean((mu - y) ** 2))),
                   "rmse_last10": float(np.sqrt(np.mean((base - y) ** 2))),
                   "brier_synthetic": float(np.mean((p - hit) ** 2)),
                   "brier_constant": float(np.mean((hit.mean() - hit) ** 2)),
                   "calibration_synthetic": calibration(p, hit)}  # fmt: skip
    return out


def select(rebuild: bool = False, adjust: str | None = None) -> dict:
    f = usable(history_features(rebuild))
    tr, va = f[f.season.isin(TRAIN)], f[f.season.isin(VALIDATE)]
    mod = M.fit(tr)
    res = {"adjustment": adjust or "full build", "train": list(TRAIN),
           "validate": list(VALIDATE), "test_untouched": list(TEST),
           "validation": score(mod, va), "coef": {
               "minutes": mod.minutes.raw_coef(),
               **{mk: r.raw_coef() for mk, r in mod.stats.items()}},
           "dispersion": {mk: d.to_dict() for mk, d in mod.disp.items()}}  # fmt: skip
    REPORTS.mkdir(parents=True, exist_ok=True)
    name = "props_selection.json" if not adjust else f"props_selection_{adjust}.json"
    (REPORTS / name).write_text(json.dumps(res, indent=1, default=float) + "\n")
    return res
