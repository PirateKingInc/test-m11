"""M18 Part D: the median-anchored nbap15 variant and its backtest (MODEL.md "M18 Part D").

- **Median regression:** quantile 0.5, the same stat terms and the same training seasons as
  nbap15's final test (2015-16 to 2022-23), fitted by iteratively reweighted least squares
  with nbap15's ridge penalty. Its median projection m is turned into a negative binomial mean
  mu with median(NB(mu)) = m and nbap15's frozen dispersion; P(over) comes from that
  distribution (with nbap15's validated Platt step where it has one).
- **Backtest:** rebounds and assists on the test seasons (2023-24 to 2025-26), at the opening
  line and price, at nbap15's tuned edge thresholds (reports/nba/props_tune.json), beside the
  original mean-based probabilities. This is a repeat look at the test seasons; nothing is
  chosen from it.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from betdata.nba.pipeline import TEST, TRAIN, VALIDATE
from betdata.nba.props import model as M
from betdata.nba.props.panel import MARKETS
from betdata.nfl.model import Ridge
from betdata.props.dist import mean_for_median

MARKETS_D = ("rebounds", "assists")
REPORT = Path("reports/nba/props_median_backtest.json")


def fit_median(X: np.ndarray, y: np.ndarray, names, lam: float, iters: int = 60,
               eps: float = 1e-3) -> Ridge:  # fmt: skip
    """Least absolute deviations (the conditional median) with a ridge penalty on the
    standardised coefficients, by IRLS."""
    X = np.nan_to_num(np.asarray(X, dtype=float))
    y = np.asarray(y, dtype=float)
    means, sds = X.mean(axis=0), X.std(axis=0)
    sds[sds < 1e-9] = 1.0
    Z = np.column_stack([np.ones(len(y)), (X - means) / sds])
    pen = lam * np.eye(Z.shape[1])
    pen[0, 0] = 0.0
    beta = np.linalg.lstsq(Z, y, rcond=None)[0]
    for _ in range(iters):
        w = 1.0 / np.maximum(np.abs(y - Z @ beta), eps)
        new = np.linalg.solve((Z * w[:, None]).T @ Z + pen, (Z * w[:, None]).T @ y)
        if np.max(np.abs(new - beta)) < 1e-6:
            beta = new
            break
        beta = new
    return Ridge(tuple(names), means, sds, beta[1:], float(beta[0]), lam)


def fit_medians(mod: M.PropModel, train: pd.DataFrame, lam: float = 10.0,
                markets=MARKETS_D) -> dict[str, Ridge]:  # fmt: skip
    m_hat = mod.predict_minutes(train)
    out = {}
    for mk in markets:
        X = M.stat_terms(train, m_hat, mk)[list(mod.stats[mk].names)].to_numpy(dtype=float)
        out[mk] = fit_median(X, train[MARKETS[mk]].to_numpy(dtype=float),
                             mod.stats[mk].names, lam)  # fmt: skip
    return out


def median_mu(mod: M.PropModel, med: dict[str, Ridge], f: pd.DataFrame, market: str,
              minutes=None) -> tuple[np.ndarray, np.ndarray]:  # fmt: skip
    """(median projection, the negative binomial mean anchored on it)."""
    m = mod.predict_minutes(f) if minutes is None else minutes
    X = M.stat_terms(f, m, market)[list(med[market].names)].to_numpy(dtype=float)
    q = np.maximum(med[market].predict(X), 0.0)
    return q, mean_for_median(mod.disp[market], q)


def backtest(conn) -> dict:
    from betdata.nba.props.live import p_over_no_push
    from betdata.nba.props.select import (
        LINE,
        REPORTS,
        bets,
        history_features,
        history_lines,
        priced,
        record,
        usable,
    )

    tune = json.loads((REPORTS / "props_tune.json").read_text())
    f = usable(history_features())
    tr = f[f.season.isin((*TRAIN, *VALIDATE))]
    mod = M.fit(tr)
    for mk, r in tune["markets"].items():
        if r.get("calibration_applied"):
            mod.calib[mk] = tuple(r["platt"])
    med = fit_medians(mod, tr)
    te = f[f.season.isin(TEST)]
    lines = history_lines(conn)
    res = {"label": "repeat look at the test seasons (M18 Part D); nothing chosen from it",
           "seasons": list(TEST), "train": [*TRAIN, *VALIDATE], "line": LINE, "markets": {}}
    for mk in MARKETS_D:
        th = (tune["markets"].get(mk) or {}).get("threshold")
        out = {"threshold": th}
        for point in ("pretip", "morning"):
            d = priced(mod, te[te.point == point], lines, mk, LINE)
            if d.empty:
                out[point] = None
                continue
            q, mu_med = median_mu(mod, med, d, mk)
            d_med = d.assign(median=q, mu=mu_med)
            d_med["p"] = p_over_no_push(mod, mk, mu_med, d.line.to_numpy())
            d_med["edge"] = 100 * (d_med.p - d_med.nv)
            ok = d.over.notna()
            row = {"rows": int(len(d))}
            for name, x, proj in (("mean_based", d, d.mu), ("median_anchored", d_med, q)):
                row[name] = {
                    "mae_projection": float(np.abs(proj - d.actual).mean()),
                    "brier": float(np.mean((x.p[ok] - x.over[ok]) ** 2)),
                    "bets": record(bets(x, th)) if th is not None else None,
                    "by_season": ({int(sn): record(bets(g, th)) for sn, g in x.groupby("season")}
                                  if th is not None else None)}  # fmt: skip
            row["brier_novig"] = float(np.mean((d.nv[ok] - d.over[ok]) ** 2))
            row["mae_line"] = float(np.abs(d.line - d.actual).mean())
            out[point] = row
        res["markets"][mk] = out
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(res, indent=1, default=float) + "\n")
    return res
