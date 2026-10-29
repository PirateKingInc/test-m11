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


def nfl() -> dict:
    """m13w (the page model, frozen design refitted on 2013-2022) on 2023-2025 at the prekick
    point; ESPN's opening and closing line of the first listed book that has both (m17-data
    branch; ESPN keeps no openers before late 2023)."""
    from sqlalchemy import select

    from betdata.db.schema import game_source_ids
    from betdata.nfl.cli import _engine, _wx_inputs
    from betdata.nfl.engine import inputs as I
    from betdata.nfl.engine import lines as L
    from betdata.nfl.engine import plays as P
    from betdata.nfl.engine import rates as R
    from betdata.nfl.pipeline import TEST, TRAIN, VALIDATE
    from betdata.nfl.selection import BET_THRESHOLDS, Design, fit_design, predict_rows
    from betdata.nfl.weather import WX_DESIGN_PATH

    _, _, rows, out = _wx_inputs()
    fits = fit_design(rows, out, Design.load(WX_DESIGN_PATH), (*TRAIN, *VALIDATE))
    pred = predict_rows(rows[rows.season.isin(TEST)], fits)
    pred = pred[pred.point == "prekick"][["game_id", "margin"]]
    with _engine().connect() as c:
        nid = dict(c.execute(select(game_source_ids.c.game_id, game_source_ids.c.source_game_id)
                             .where(game_source_ids.c.source == "nflverse")).all())  # fmt: skip
    pred["nid"] = pred.game_id.map(nid)
    es = L.espn(R.RateStore(P.load(range(2022, 2026))), I.team_ids(), TEST)
    d = pred.merge(es, left_on="nid", right_index=True).merge(
        out.rename(columns={"margin": "act_margin"})[["act_margin"]], left_on="game_id",
        right_index=True)  # fmt: skip
    d["season"] = d.nid.str[:4].astype(int)
    return {"sport": "nfl", "model": "m13w (refit 2013-2022)", "point": "prekick",
            "seasons": list(TEST), "lines": "ESPN, first listed book with open and close",
            "games": int(len(d)), "threshold": BET_THRESHOLDS["spread"],
            **summarize(picks(d, BET_THRESHOLDS["spread"]))}  # fmt: skip


def nba() -> dict:
    """nba15 (the page model) out of sample at the pretip point: 2021-22..2022-23 fitted on
    2015-16..2020-21 and 2023-24..2025-26 fitted on 2015-16..2022-23; the stored opening and
    closing lines (`market_frame`)."""
    import json

    from betdata.config import PROJECT_ROOT
    from betdata.nba.pipeline import TEST, TRAIN, VALIDATE, columns
    from betdata.nba.select15 import rows15
    from betdata.nba.selection import fits_for, market_frame, predictions

    d = json.loads((PROJECT_ROOT / "config" / "nba15_model.json").read_text())
    store, out, rows, _ = rows15()
    m, t = columns(rows, d["groups"])
    parts = [predictions(rows, out, fits_for(rows, out, m, t, f, d["lam_m"], d["lam_t"]), s)
             for f, s in ((TRAIN, VALIDATE), (TRAIN + VALIDATE, TEST))]  # fmt: skip
    p = pd.concat(parts).join(market_frame(store, rows.game_id.unique()), on="game_id")
    p = p[p.point == "pretip"]
    x = pd.DataFrame({"season": p.season, "margin": p.pred, "open_spread": p.open_spread_home,
                      "close_spread": p.close_spread_home, "act_margin": p.margin})
    res = {"sport": "nba", "model": "nba15", "point": "pretip",
           "threshold": d["spread_threshold"], "games": int(x.dropna().shape[0])}
    for label, ss in (("validation", VALIDATE), ("test", TEST), ("both", VALIDATE + TEST)):
        res[label] = summarize(picks(x[x.season.isin(ss)], d["spread_threshold"]))
    return res
