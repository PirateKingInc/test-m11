"""NFL prop model on validation, the one test scoring, and the frozen fit (MODEL.md, M15).

No NFL prop lines exist for the validation seasons (2020-2022), so there is no betting backtest:
each market is judged on prediction error (MAE/RMSE) against a naive baseline (the last-10-game
average; for anytime TD the player's season TD rate) and on the calibration of P(over) at a
synthetic line (the last-10 average rounded down + 0.5; anytime TD: P(at least one)).

Decisions made on validation (fit 2013-2019, score 2020-2022) and frozen for the test and live:
  yards family    negative binomial vs normal, the lower validation Brier at the synthetic line
  calibration     one Platt fit per market on the validation predictions, applied only when the
                  market is miscalibrated: a calibration bin of >= 300 predictions off by more
                  than 0.03, or an n-weighted mean gap above 0.015
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from betdata.config import PROJECT_ROOT
from betdata.nfl.pipeline import TEST, TRAIN, VALIDATE
from betdata.nfl.props import model as M
from betdata.nfl.props.history import history_features

log = logging.getLogger(__name__)
REPORTS = PROJECT_ROOT / "reports" / "nfl"
SELECTION = REPORTS / "props_selection.json"
FINAL = REPORTS / "props_final_test.json"
BINS = (0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
MISCAL_BIN, MISCAL_N, MISCAL_MEAN = 0.03, 300, 0.015
FEWER_TERMS = {
    "rec_yds": ("be", "br", "bair", "redis", "opp", "wind", "mz", "own_q", "g10"),
    "receptions": ("be", "br", "redis", "opp", "wind", "own_q", "g10"),
    "rush_yds": ("be", "redis", "opp", "own_q", "g10"),
    "pass_yds": ("be", "mates", "opp", "wind", "own_q", "g10"),
    "anytime_td": ("rz6", "rz_imp", "redis", "td16", "own_q"),
}
# The full build and the adjustments tried on validation (3, the same for every market; each
# market keeps the variant with the lowest validation error), by name: fit options.
ADJUSTMENTS: dict[str, dict] = {
    "full build": {},
    "1 ridge penalty 100": {"lam_stat": 100.0},
    "2 ridge penalty 1000": {"lam_stat": 1000.0},
    "3 fewer share terms (EW and route shares only)": {"term_sets": FEWER_TERMS},
}


def calibration(p: np.ndarray, y: np.ndarray) -> list[dict]:
    b = pd.cut(p, list(BINS), include_lowest=True)
    t = pd.DataFrame({"p": p, "y": y, "b": b}).groupby("b", observed=True).agg(
        n=("y", "size"), pred=("p", "mean"), actual=("y", "mean"))  # fmt: skip
    return [{"bin": str(i), "n": int(r.n), "pred": round(float(r.pred), 3),
             "actual": round(float(r.actual), 3)} for i, r in t.iterrows()]  # fmt: skip


def miscalibrated(cal: list[dict]) -> bool:
    n = sum(c["n"] for c in cal)
    mean_gap = sum(c["n"] * abs(c["pred"] - c["actual"]) for c in cal) / max(n, 1)
    worst = any(c["n"] >= MISCAL_N and abs(c["pred"] - c["actual"]) > MISCAL_BIN for c in cal)
    return worst or mean_gap > MISCAL_MEAN


def platt(p: np.ndarray, y: np.ndarray, iters: int = 50) -> tuple[float, float]:
    """Logistic regression of the outcome on logit(p) (Newton's method): (slope, intercept)."""
    q = np.clip(p, 1e-6, 1 - 1e-6)
    x = np.log(q / (1 - q))
    w = np.array([1.0, 0.0])
    X = np.vstack([x, np.ones_like(x)]).T
    for _ in range(iters):
        z = 1 / (1 + np.exp(-(X @ w)))
        g = X.T @ (y - z)
        h = (X * (z * (1 - z))[:, None]).T @ X + 1e-9 * np.eye(2)
        w = w + np.linalg.solve(h, g)
    return float(w[0]), float(w[1])


def synthetic_line(d: pd.DataFrame, market: str) -> np.ndarray:
    if market == "anytime_td":
        return np.full(len(d), 0.5)
    return np.floor(d[f"g10_{M.MARKETS[market]}"].to_numpy(dtype=float)) + 0.5


def baseline(d: pd.DataFrame, market: str) -> np.ndarray:
    if market == "anytime_td":
        return d.sn_td.fillna(0).to_numpy(dtype=float)
    return d[f"g10_{M.MARKETS[market]}"].to_numpy(dtype=float)


def _err(x, y) -> dict:
    return {"mae": float(np.mean(np.abs(x - y))), "rmse": float(np.sqrt(np.mean((x - y) ** 2)))}


def score_market(mod: M.PropModel, d: pd.DataFrame, market: str) -> dict:
    """Error vs the baseline and P(over) calibration at the synthetic line (every yards family
    for yards markets, so the family can be chosen on validation)."""
    y = d[f"y_{M.MARKETS[market]}"].to_numpy(dtype=float)
    mu = mod.predict(d, market)
    base = baseline(d, market)
    line = synthetic_line(d, market)
    hit = (y > line).astype(float)
    out = {"n": int(len(y)), "model": _err(mu, y), "baseline": _err(base, y),
           "brier_constant": float(np.mean((hit.mean() - hit) ** 2))}  # fmt: skip
    if market == "anytime_td":
        p = mod.p_over(market, mu, line)
        pb = 1 - np.exp(-np.maximum(base, 0.0))
        out.update(brier=float(np.mean((p - hit) ** 2)), brier_baseline=float(
            np.mean((pb - hit) ** 2)), calibration=calibration(p, hit),
            raw_calibration=calibration(mod.raw_p_over(market, mu, line), hit))  # fmt: skip
        return out
    fams = ("negbin", "normal") if market in M.YARDS else (None,)
    for fam in fams:
        dist = mod.dists[market]
        if fam is not None:
            dist = M.YardsDist(fam, dist.disp)
        p = dist.p_over(mu, line)
        key = fam or "negbin"
        out[f"brier_{key}"] = float(np.mean((p - hit) ** 2))
        out[f"calibration_{key}"] = calibration(p, hit)
    if market in mod.calib:
        out["brier_platt"] = float(np.mean((mod.p_over(market, mu, line) - hit) ** 2))
    return out


def split(f: pd.DataFrame, seasons) -> pd.DataFrame:
    return f[f.season.isin(list(seasons)) & f.has_state.astype(bool)]




def _judge(r: dict, market: str) -> float:
    """What a variant is judged on: validation MAE; for anytime TD the Brier score of
    P(anytime), since the MAE of a count that is mostly 0 rewards predicting 0."""
    return r["brier"] if market == "anytime_td" else r["model"]["mae"]


def decide(f: pd.DataFrame, train, valid) -> tuple[dict, dict]:
    """Every variant fitted on `train` and scored on `valid`; per market the variant with the
    lowest validation error. Returns (per-variant scores, per-market options)."""
    tr, va = split(f, train), split(f, valid)
    pops = M.populations(va)
    runs = {}
    for name, opts in ADJUSTMENTS.items():
        mod = M.fit(tr, **opts)
        runs[name] = {mk: score_market(mod, va[pops[mk]], mk) for mk in M.MARKETS}
    best = {mk: min(runs, key=lambda n, mk=mk: _judge(runs[n][mk], mk)) for mk in M.MARKETS}
    lam = {mk: ADJUSTMENTS[n].get("lam_stat", 10.0) for mk, n in best.items()}
    terms = {mk: list(ADJUSTMENTS[n].get("term_sets", {}).get(mk, M.TERMS[mk]))
             for mk, n in best.items()}  # fmt: skip
    return runs, {"variant": best, "lam_stat": lam, "term_sets": terms}


def _hits(mod: M.PropModel, d: pd.DataFrame, mk: str, calibrated: bool = True):
    line = synthetic_line(d, mk)
    mu = mod.predict(d, mk)
    p = mod.p_over(mk, mu, line) if calibrated else mod.raw_p_over(mk, mu, line)
    y = d[f"y_{M.MARKETS[mk]}"].to_numpy(dtype=float)
    return p, (y > line).astype(float)


def select(rebuild: bool = False) -> dict:
    f = history_features(rebuild)
    runs, opts = decide(f, TRAIN, VALIDATE)
    tr, va = split(f, TRAIN), split(f, VALIDATE)
    mod = M.fit(tr, lam_stat=opts["lam_stat"], term_sets=opts["term_sets"])
    pops = M.populations(va)
    val = {mk: score_market(mod, va[pops[mk]], mk) for mk in M.MARKETS}
    families = {mk: min(("negbin", "normal"), key=lambda fam, mk=mk: val[mk][f"brier_{fam}"])
                for mk in M.YARDS}  # fmt: skip
    mod.dists.update({k: M.YardsDist(fam, mod.dists[k].disp) for k, fam in families.items()})
    calib = {}
    for mk, r in val.items():
        cal = r["calibration" if mk == "anytime_td" else
                f"calibration_{families.get(mk, 'negbin')}"]  # fmt: skip
        r["miscalibrated"] = miscalibrated(cal)
        if r["miscalibrated"]:
            calib[mk] = platt(*_hits(mod, va[pops[mk]], mk, calibrated=False))
    mod.calib = calib
    for mk in calib:  # after the Platt fit (in-sample on validation: a check only)
        p, hit = _hits(mod, va[pops[mk]], mk)
        val[mk]["brier_platt"] = float(np.mean((p - hit) ** 2))
        val[mk]["calibration_after_platt"] = calibration(p, hit)
    res = {"train": list(TRAIN), "validate": list(VALIDATE), "test_untouched": list(TEST),
           "adjustments": ADJUSTMENTS,
           "variants": {n: {mk: {"model": r["model"], "baseline": r["baseline"],
                                 "brier": r.get("brier", r.get("brier_negbin"))}
                            for mk, r in rr.items()} for n, rr in runs.items()},
           "decisions": {**opts, "families": families,
                         "platt": {k: list(v) for k, v in calib.items()}},
           "validation": val, "team_volume": {k: r.raw_coef() for k, r in mod.team.items()},
           "coef": {mk: r.raw_coef() for mk, r in mod.stats.items()},
           "dists": {mk: d.to_dict() for mk, d in mod.dists.items()}}  # fmt: skip
    REPORTS.mkdir(parents=True, exist_ok=True)
    SELECTION.write_text(json.dumps(res, indent=1, default=float) + "\n")
    return res


def chosen(path: Path = SELECTION) -> dict:
    """The decisions recorded by the selection report (what the test and live fits use)."""
    d = json.loads(path.read_text())["decisions"]
    return {"lam_stat": d["lam_stat"], "term_sets": d["term_sets"], "families": d["families"],
            "platt": {k: tuple(v) for k, v in d["platt"].items()}}  # fmt: skip


def fit_frozen(f: pd.DataFrame, seasons, decisions: dict) -> M.PropModel:
    mod = M.fit(split(f, seasons), families=decisions["families"],
                lam_stat=decisions["lam_stat"], term_sets=decisions["term_sets"])  # fmt: skip
    mod.calib = dict(decisions["platt"])
    return mod
