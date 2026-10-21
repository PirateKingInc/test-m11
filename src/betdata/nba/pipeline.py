"""Build NBA feature rows in time order, learn play probabilities on training seasons, fit and
evaluate ridge models per prediction point (MODEL.md, M14 "Seasons and acceptance rule")."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from betdata.nba.features import GROUPS, status_key, streak_for, team_status
from betdata.nba.points import POINTS, point_time
from betdata.nba.state import League
from betdata.nfl.features.state import advanced
from betdata.nfl.model import Ridge, fit_ridge
from betdata.nfl.store import AsOf, Store

log = logging.getLogger(__name__)

TRAIN = tuple(range(2015, 2021))
VALIDATE = (2021, 2022)
TEST = (2023, 2024, 2025)
LAMBDAS = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0)
MIN_COUNT = 30  # status buckets seen fewer times in training keep the default


def resolve(names: str | Sequence[str]) -> list:
    names = names.split(",") if isinstance(names, str) else list(names)
    out = [GROUPS["base"]]
    for n in names:
        n = n.strip()
        if n and n != "base":
            if n not in GROUPS:
                raise ValueError(f"unknown NBA feature group {n!r}; available: {list(GROUPS)}")
            out.append(GROUPS[n])
    return out


def tasks(store: Store, game_ids: Iterable[int], points: Sequence[str] = POINTS):
    return sorted((point_time(p, store.games.at[gid, "tip"]), gid, p)
                  for gid in game_ids for p in points)  # fmt: skip


def game_features(store: Store, groups, game_id: int, point: str, t=None) -> dict[str, float]:
    g = store.games.loc[game_id]
    a = AsOf(store, point_time(point, g.tip) if t is None else t)
    out: dict[str, float] = {}
    for grp in groups:
        out.update(grp.compute(a, g, point))
    return out


def build(store: Store, groups, game_ids: Iterable[int],
          points: Sequence[str] = POINTS) -> pd.DataFrame:  # fmt: skip
    rows = []
    todo = tasks(store, game_ids, points)
    for i, (t, gid, p) in enumerate(todo):
        g = store.games.loc[gid]
        a = AsOf(store, t)
        row = {"game_id": gid, "point": p, "season": g.season}
        for grp in groups:
            row.update(grp.compute(a, g, p))
        rows.append(row)
        if i and i % 5000 == 0:
            log.info("features: %d/%d", i, len(todo))
    return pd.DataFrame(rows)


def played(store: Store) -> dict[int, set[str]]:
    """game_id -> players with minutes (post-game facts: for fitting only)."""
    out = {}
    for r in store.frame("box").df.itertuples():
        out[r.game_id] = {ln[1] for ln in r.players if ln[2] > 0}
    return out


def fit_play_probs(store: Store, seasons: Sequence[int] = TRAIN) -> dict[str, float]:
    """P(plays) per report status (or, off the report, per games missed in a row), from every
    rotation player at both prediction points of the training seasons."""
    who = played(store)
    counts: dict[str, list[int]] = {}
    gids = [gid for gid, s in store.games.season.items() if s in seasons and gid in who]
    for t, gid, _p in tasks(store, gids):
        g = store.games.loc[gid]
        a = AsOf(store, t)
        st = advanced(a, "league", League)
        for team in (g.home, g.away):
            status, reported = team_status(a, g, team)
            for pid, pl in st.rotation(team):
                key = status_key(status.get(pid), streak_for(st, g, pl), reported)
                c = counts.setdefault(key, [0, 0])
                c[0] += pid in who[gid]
                c[1] += 1
    return {k: (y + 1) / (n + 2) for k, (y, n) in counts.items() if n >= MIN_COUNT} | {
        f"n:{k}": float(n) for k, (_, n) in counts.items()}


@dataclass
class Fit:
    point: str
    margin: Ridge
    total: Ridge
    sigma: float

    def predict(self, x: dict[str, float]) -> tuple[float, float]:
        return self.margin.predict_one(x), self.total.predict_one(x)


def _xy(rows, out, cols, target):
    d = rows.join(out, on="game_id", how="inner")
    return d[cols].to_numpy(dtype=float), d[target].to_numpy(dtype=float), d


def fit_point(rows, out, point, train, m_cols, t_cols, lam_m, lam_t) -> Fit:
    r = rows[(rows.point == point) & rows.season.isin(train)]
    Xm, ym, _ = _xy(r, out, m_cols, "margin")
    Xt, yt, _ = _xy(r, out, t_cols, "total")
    m = fit_ridge(Xm, ym, m_cols, lam_m)
    t = fit_ridge(Xt, yt, t_cols, lam_t)
    return Fit(point, m, t, float(np.std(ym - m.predict(np.nan_to_num(Xm)))))


def mae(rows, out, fit: Fit, seasons) -> tuple[float, float, int]:
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
    valid: dict[str, tuple[float, float, int]]

    @property
    def margin(self) -> float:
        return float(np.mean([v[0] for v in self.valid.values()]))

    @property
    def total(self) -> float:
        return float(np.mean([v[1] for v in self.valid.values()]))


def evaluate(rows, out, m_cols, t_cols, train=TRAIN, valid=VALIDATE,
             lambdas=LAMBDAS) -> Evaluation:  # fmt: skip
    best = {}
    for which in ("m", "t"):
        for lam in lambdas:
            fits = [fit_point(rows, out, p, train, m_cols, t_cols, lam, lam) for p in POINTS]
            e = np.mean([mae(rows, out, f, valid)[0 if which == "m" else 1] for f in fits])
            if which not in best or e < best[which][0]:
                best[which] = (e, lam)
    lam_m, lam_t = best["m"][1], best["t"][1]
    fits = {p: fit_point(rows, out, p, train, m_cols, t_cols, lam_m, lam_t) for p in POINTS}
    return Evaluation(lam_m, lam_t, {p: mae(rows, out, f, valid) for p, f in fits.items()})


def columns(rows: pd.DataFrame, groups: Sequence[str]) -> tuple[list[str], list[str]]:
    keep = ["base", *groups]
    m = [c for c in rows.columns if c.startswith("m_") and c.split("_")[1] in keep]
    t = [c for c in rows.columns if c.startswith("t_") and c.split("_")[1] in keep]
    return m, t
