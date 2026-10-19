"""Build feature rows for many games in time order, fit, and evaluate on held-out seasons."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from betdata.nfl.model import Ridge, fit_ridge
from betdata.nfl.points import POINTS, point_time
from betdata.nfl.store import AsOf, Store

log = logging.getLogger(__name__)

TRAIN = tuple(range(2013, 2020))
VALIDATE = (2020, 2021, 2022)
TEST = (2023, 2024, 2025)
LAMBDAS = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0, 3000.0)


def game_features(store: Store, groups, game_id: int, point: str, t=None) -> dict[str, float]:
    g = store.games.loc[game_id]
    a = AsOf(store, point_time(point, g.kickoff) if t is None else t)
    out: dict[str, float] = {}
    for grp in groups:
        out.update(grp.compute(a, g, point))
    return out


def build(store: Store, groups, game_ids: Iterable[int],
          points: Sequence[str] = POINTS) -> pd.DataFrame:  # fmt: skip
    """One row per (game, point): features as known at that point. Rows are computed in time
    order so incremental caches (Elo, ratings) only ever move forward."""
    tasks = sorted(
        (point_time(p, store.games.at[gid, "kickoff"]), gid, p) for gid in game_ids for p in points
    )
    rows = []
    for i, (t, gid, p) in enumerate(tasks):
        g = store.games.loc[gid]
        a = AsOf(store, t)
        row = {"game_id": gid, "point": p, "season": g.season, "week": g.week}
        for grp in groups:
            row.update(grp.compute(a, g, p))
        rows.append(row)
        if i and i % 2000 == 0:
            log.info("features: %d/%d", i, len(tasks))
    return pd.DataFrame(rows)


def outcomes(store: Store) -> pd.DataFrame:
    """Final scores, for fitting and scoring only (never a feature)."""
    r = store.frame("results").df.set_index("game_id")
    return pd.DataFrame(
        {"margin": r.home_score - r.away_score, "total": r.home_score + r.away_score}
    )


def names(rows: pd.DataFrame, prefix: str, exclude: Iterable[str] = ()) -> list[str]:
    drop = set(exclude)
    return [c for c in rows.columns if c.startswith(prefix) and c not in drop]


@dataclass
class Fit:
    point: str
    margin: Ridge
    total: Ridge
    sigma: float  # margin residual sd on training seasons

    def predict(self, x: dict[str, float]) -> tuple[float, float]:
        return self.margin.predict_one(x), self.total.predict_one(x)


def _xy(rows, out, cols, target):
    d = rows.join(out, on="game_id", how="inner")
    return d[cols].to_numpy(dtype=float), d[target].to_numpy(dtype=float), d


def fit_point(rows: pd.DataFrame, out: pd.DataFrame, point: str, train: Sequence[int],
              m_cols: list[str], t_cols: list[str], lam_m: float, lam_t: float) -> Fit:  # fmt: skip
    r = rows[(rows.point == point) & rows.season.isin(train)]
    Xm, ym, _ = _xy(r, out, m_cols, "margin")
    Xt, yt, _ = _xy(r, out, t_cols, "total")
    m = fit_ridge(Xm, ym, m_cols, lam_m)
    t = fit_ridge(Xt, yt, t_cols, lam_t)
    sigma = float(np.std(ym - m.predict(np.nan_to_num(Xm))))
    return Fit(point, m, t, sigma)


def mae(rows, out, fit: Fit, seasons: Sequence[int]) -> tuple[float, float, int]:
    r = rows[(rows.point == fit.point) & rows.season.isin(seasons)]
    Xm, ym, _ = _xy(r, out, list(fit.margin.names), "margin")
    Xt, yt, _ = _xy(r, out, list(fit.total.names), "total")
    em = np.abs(fit.margin.predict(np.nan_to_num(Xm)) - ym).mean()
    et = np.abs(fit.total.predict(np.nan_to_num(Xt)) - yt).mean()
    return float(em), float(et), len(ym)


@dataclass
class Evaluation:
    lam_m: float
    lam_t: float
    valid: dict[str, tuple[float, float, int]]  # point -> (margin MAE, total MAE, games)
    train: dict[str, tuple[float, float, int]]

    @property
    def margin(self) -> float:
        return float(np.mean([v[0] for v in self.valid.values()]))

    @property
    def total(self) -> float:
        return float(np.mean([v[1] for v in self.valid.values()]))


def evaluate(rows: pd.DataFrame, out: pd.DataFrame, m_cols: list[str], t_cols: list[str],
             train: Sequence[int] = TRAIN, valid: Sequence[int] = VALIDATE,
             lambdas: Sequence[float] = LAMBDAS) -> Evaluation:  # fmt: skip
    """Fit on `train` for each point; the ridge penalty is chosen by validation MAE (margin and
    total separately, the same penalty for both points)."""
    best_m, best_t = None, None
    for lam in lambdas:
        fits = [fit_point(rows, out, p, train, m_cols, t_cols, lam, lam) for p in POINTS]
        em = np.mean([mae(rows, out, f, valid)[0] for f in fits])
        et = np.mean([mae(rows, out, f, valid)[1] for f in fits])
        if best_m is None or em < best_m[0]:
            best_m = (em, lam)
        if best_t is None or et < best_t[0]:
            best_t = (et, lam)
    lam_m, lam_t = best_m[1], best_t[1]
    fits = {p: fit_point(rows, out, p, train, m_cols, t_cols, lam_m, lam_t) for p in POINTS}
    return Evaluation(
        lam_m, lam_t,
        {p: mae(rows, out, f, valid) for p, f in fits.items()},
        {p: mae(rows, out, f, train) for p, f in fits.items()},
    )  # fmt: skip
