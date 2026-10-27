"""M17 Part D: engine vs market (MODEL.md "M17" Part D). The line is read only here, after
the engine has predicted.

  gap            engine home margin + market home spread (+ = the engine likes home more);
                 total: engine total - line
  p_cover        P(home covers) from the simulated margin distribution (pushes excluded)
  decomposition  the gap's contribution of each chain step: the full prediction minus a re-run
                 with that step at the league average (same random numbers)
  thresholds     chosen on validation only: the grid edge with the best ROI at -110 among those
                 with >= 100 picks, if positive
  tests          (1) gaps vs results, (2) gaps at the opening line vs the move to the close,
                 (3) which steps the closing line agrees with
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SIDE_GRID = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0)
PROP_GRID = (0.04, 0.06, 0.08, 0.10, 0.12)
WIN_110 = 100 / 110


def p_cover(hist: np.ndarray, spread_home: float) -> float:
    """P(margin + spread > 0 | not a push) from a margin histogram (index = margin + 60)."""
    m = np.arange(len(hist)) - 60
    v = m + spread_home
    win, lose = hist[v > 0].sum(), hist[v < 0].sum()
    return float(win / (win + lose)) if win + lose else 0.5


def side_bets(df: pd.DataFrame, threshold: float, line: str = "close_spread_home") -> pd.DataFrame:
    """One pick per game where |gap| >= threshold: side, result (1 win, 0 loss, push NaN) and
    profit at -110."""
    d = df[df[line].notna() & df.act_margin.notna()].copy()
    d["gap"] = d.margin_mean + d[line]
    d = d[d.gap.abs() >= threshold]
    cover = d.act_margin + d[line]
    home = d.gap > 0
    res = np.where(cover == 0, np.nan, np.where(home, cover > 0, cover < 0).astype(float))
    d["result"] = res
    d["profit"] = np.where(np.isnan(res), 0.0, np.where(res == 1, WIN_110, -1.0))
    return d


def record(b: pd.DataFrame) -> dict:
    settled = b[b.result.notna()]
    n = len(settled)
    prof = settled.profit.to_numpy()
    roi = float(prof.mean()) if n else None
    se = float(prof.std(ddof=1) / np.sqrt(n)) if n > 1 else None
    return {"picks": int(len(b)), "wins": int((settled.result == 1).sum()),
            "losses": int((settled.result == 0).sum()), "pushes": int(b.result.isna().sum()),
            "roi": roi, "roi_95": [roi - 1.96 * se, roi + 1.96 * se] if se else None}


def choose_threshold(val: pd.DataFrame, grid=SIDE_GRID, min_picks: int = 100) -> dict:
    rows = []
    for t in grid:
        r = record(side_bets(val, t))
        rows.append({"threshold": t, **r})
    ok = [r for r in rows if r["picks"] >= min_picks and r["roi"] is not None]
    best = max(ok, key=lambda r: r["roi"]) if ok else None
    chosen = best["threshold"] if best and best["roi"] > 0 else None
    return {"grid": rows, "chosen": chosen}


def test_results(df: pd.DataFrame, line: str = "close_spread_home") -> dict:
    """Test 1: do gaps predict results? Cover rate by gap size and a logistic slope."""
    d = side_bets(df, 0.0, line)
    d = d[d.result.notna()]
    d["absgap"] = d.gap.abs()
    bins = pd.cut(d.absgap, [0, 1, 2, 3, 4, 6, 100], right=False)
    by = d.groupby(bins, observed=True).agg(n=("result", "size"), cover=("result", "mean"),
                                            roi=("profit", "mean"))
    # logistic of "home covers" on the gap (home side's point of view)
    y = ((d.act_margin + d[line]) > 0).astype(float).to_numpy()
    x = d.gap.to_numpy()
    b = _logit_fit(x, y)
    return {"n": int(len(d)), "by_abs_gap": {str(k): v for k, v in by.round(4).to_dict(
        "index").items()}, "logit_slope_per_point": b[1], "slope_se": b[2]}


def _logit_fit(x, y):
    """Logistic regression y ~ a + b x by Newton; returns (a, b, se_b)."""
    X = np.column_stack([np.ones(len(x)), x])
    w = np.zeros(2)
    for _ in range(50):
        p = 1 / (1 + np.exp(-X @ w))
        H = X.T @ (X * (p * (1 - p))[:, None])
        g = X.T @ (y - p)
        step = np.linalg.solve(H + 1e-9 * np.eye(2), g)
        w += step
        if np.abs(step).max() < 1e-10:
            break
    cov = np.linalg.inv(H)
    return float(w[0]), float(w[1]), float(np.sqrt(cov[1, 1]))


def test_clv(df: pd.DataFrame) -> dict:
    """Test 2: does the gap at the opening line predict the move to the close (toward the
    engine)? `open_spread`/`close_spread`: ESPN home spreads; gap at the open."""
    d = df[df.open_spread.notna() & df.close_spread.notna()].copy()
    d["gap_open"] = d.margin_mean + d.open_spread
    d["move_to_home"] = d.open_spread - d.close_spread  # + = the market moved toward home
    x, y = d.gap_open.to_numpy(), d.move_to_home.to_numpy()
    X = np.column_stack([np.ones(len(x)), x])
    beta, res, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    s2 = resid @ resid / max(len(y) - 2, 1)
    se = float(np.sqrt(s2 * np.linalg.inv(X.T @ X)[1, 1]))
    out = {"n": int(len(d)), "slope": float(beta[1]), "slope_se": se,
           "moved": float((d.move_to_home != 0).mean())}  # fmt: skip
    for t in SIDE_GRID:
        big = d[d.gap_open.abs() >= t]
        moved = big[big.move_to_home != 0]
        toward = (np.sign(moved.move_to_home) == np.sign(moved.gap_open)).mean()
        out[f"toward_engine_gap_ge_{t:g}"] = {"n": int(len(big)), "moved": int(len(moved)),
                                               "share_toward": float(toward) if len(moved)
                                               else None}  # fmt: skip
    return out


def test_agreement(df: pd.DataFrame, steps, line: str = "close_spread_home") -> dict:
    """Test 3: regress the closing market margin (-spread) and the cover margin (result +
    spread) on the chain-step contributions (`c_<step>`), with a constant."""
    d = df.dropna(subset=[line, "act_margin"] + [f"c_{s}" for s in steps])
    X = np.column_stack([np.ones(len(d))] + [d[f"c_{s}"].to_numpy() for s in steps])
    out = {"n": int(len(d))}
    for name, y in (("market", -d[line].to_numpy()),
                    ("cover_margin", (d.act_margin + d[line]).to_numpy())):
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ beta
        s2 = resid @ resid / max(len(y) - X.shape[1], 1)
        se = np.sqrt(np.diag(s2 * np.linalg.pinv(X.T @ X)))
        out[name] = {s: {"coef": float(beta[i + 1]), "se": float(se[i + 1])}
                     for i, s in enumerate(steps)}  # fmt: skip
    return out


def prop_bets(df: pd.DataFrame, threshold: float, kind: str = "close") -> pd.DataFrame:
    """Prop picks where |P(over) - no-vig over| >= threshold, at the `kind` (open/close) line
    and prices; result by the actual stat."""
    from betdata.moneyline import no_vig

    d = df[df[f"{kind}_line"].notna() & df[f"{kind}_over_price"].notna()
           & df[f"{kind}_under_price"].notna() & df.actual.notna()].copy()  # fmt: skip
    nv = [no_vig(o, u)[0] for o, u in zip(d[f"{kind}_over_price"], d[f"{kind}_under_price"],
                                         strict=True)]  # fmt: skip
    d["no_vig_over"] = nv
    d["edge"] = d.p_over - d.no_vig_over
    d = d[d.edge.abs() >= threshold]
    over = d.edge > 0
    line = d[f"{kind}_line"]
    res = np.where(d.actual == line, np.nan,
                   np.where(over, d.actual > line, d.actual < line).astype(float))
    price = np.where(over, d[f"{kind}_over_price"], d[f"{kind}_under_price"]).astype(float)
    win = np.where(price > 0, price / 100, 100 / np.abs(price))
    d["result"] = res
    d["profit"] = np.where(np.isnan(res), 0.0, np.where(res == 1, win, -1.0))
    return d
