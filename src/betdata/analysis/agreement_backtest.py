"""Backtest of the spread agreement rule (MODEL.md "Spread flags: the agreement rule").

On stored history only signal (a) can be tested: the line moved toward the model's side since
the open. Picks are taken at the close (the backtest's pick time): a threshold pick when
|model margin + closing home spread| >= the threshold; an agreement pick when, in addition, the
closing line moved from the opening line toward the picked side. Graded at -110. Pinnacle (b)
has no history (polled live from 2026-10-23).
"""

from __future__ import annotations

import math

import pandas as pd

WIN_110 = 100 / 110


def picks(df: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """`df`: margin (model home margin), open_spread, close_spread (home, sportsbook
    convention), act_margin, season. One row per threshold pick with side, moved_toward,
    result (1 win, 0 loss, NaN push) and profit."""
    d = df.dropna(subset=["margin", "open_spread", "close_spread", "act_margin"]).copy()
    d["edge"] = d.margin + d.close_spread
    d = d[d.edge.abs() >= threshold].copy()
    home = d.edge > 0
    d["side"] = home.map({True: "home", False: "away"})
    # toward home: the home spread fell (home favoured more); toward away: it rose
    d["moved_toward"] = (home & (d.close_spread < d.open_spread)) | (
        ~home & (d.close_spread > d.open_spread))
    cover = d.act_margin + d.close_spread
    d["result"] = [math.nan if c == 0 else float((c > 0) == h) for c, h in zip(cover, home,
                                                                                 strict=True)]
    d["profit"] = d.result.map(lambda r: 0.0 if r != r else (WIN_110 if r == 1 else -1.0))
    return d


def wilson(w: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = w / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (mid - half, mid + half)


def record(d: pd.DataFrame) -> dict:
    s = d[d.result.notna()]
    w, n = int((s.result == 1).sum()), len(s)
    roi = float(d.profit.sum() / n) if n else None
    se = float(s.profit.std(ddof=1) / math.sqrt(n)) if n > 1 else None
    ci = wilson(w, n)
    return {"picks": int(len(d)), "wins": w, "losses": n - w,
            "pushes": int(d.result.isna().sum()),
            "win_rate": w / n if n else None,
            "win_rate_95": [round(x, 3) for x in ci] if ci else None,
            "roi": roi, "roi_95": [roi - 1.96 * se, roi + 1.96 * se] if se else None}


def summarize(d: pd.DataFrame) -> dict:
    out = {"all_threshold": record(d), "agreement": record(d[d.moved_toward]),
           "no_agreement": record(d[~d.moved_toward]), "by_season": {}}
    for s, x in d.groupby("season"):
        out["by_season"][str(s)] = {"all_threshold": record(x),
                                    "agreement": record(x[x.moved_toward])}
    return out
