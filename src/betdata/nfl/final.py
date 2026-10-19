"""The one scoring of the test seasons (2023-2025) and the replay of the live weeks.

final_test: refit the frozen design on 2013-2022, predict 2023-2025 at both points, and compare
with Elo (M10 settings), M11 units+missing (fitted 2013-2019 as in M11) and the closing line:
margin and total error, calibration, and bets against the closing line at the existing
thresholds (3 points on spreads, 4 on totals) with a 95% range and ROI. The result is written to
data/reports/m12_final_test.json; a second run refuses unless forced, so the test seasons are
scored once.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from betdata.analysis.backtest import DEFAULT_PRICE, profit, walk_forward
from betdata.analysis.models import EloModel
from betdata.nfl.pipeline import TEST, TRAIN, VALIDATE
from betdata.nfl.points import POINTS
from betdata.nfl.selection import (
    BET_THRESHOLDS,
    Design,
    calibration,
    fit_design,
    predict_rows,
    wilson,
)

ELO_M10 = dict(k=20.0, hfa=60.0, revert=0.5, bye=25.0)


@dataclass
class BetLine:
    label: str
    wins: int
    losses: int
    pushes: int
    profit: float

    @property
    def bets(self) -> int:
        return self.wins + self.losses + self.pushes

    def as_dict(self) -> dict:
        ci = wilson(self.wins, self.wins + self.losses)
        return dict(label=self.label, record=f"{self.wins}-{self.losses}-{self.pushes}",
                    bets=self.bets,
                    win_rate=self.wins / max(self.wins + self.losses, 1),
                    ci95=ci, roi=self.profit / self.bets if self.bets else None)  # fmt: skip


def bets_at_close(pred_margin, pred_total, close_spread, close_total, home, away,
                  spread_t=BET_THRESHOLDS["spread"], total_t=BET_THRESHOLDS["total"]):  # fmt: skip
    """Spread and total results ('win'/'loss'/'push' or None) against the closing line."""
    out = {"spread": None, "total": None}
    if pred_margin is not None and close_spread is not None:
        edge = pred_margin + close_spread
        if abs(edge) >= spread_t:
            margin = (home - away) if edge > 0 else (away - home)
            line = close_spread if edge > 0 else -close_spread
            c = margin + line
            out["spread"] = "win" if c > 0 else "loss" if c < 0 else "push"
    if pred_total is not None and close_total is not None:
        edge = pred_total - close_total
        if abs(edge) >= total_t:
            pts = home + away
            c = pts - close_total if edge > 0 else close_total - pts
            out["total"] = "win" if c > 0 else "loss" if c < 0 else "push"
    return out


def tally(label: str, results: list[str | None]) -> BetLine:
    r = [x for x in results if x]
    return BetLine(label, r.count("win"), r.count("loss"), r.count("push"),
                   sum(profit(x, DEFAULT_PRICE) for x in r))  # fmt: skip


def final_test(rows: pd.DataFrame, out: pd.DataFrame, design: Design, games, units_preds,
               report_path: Path, force: bool = False, fit_seasons=(*TRAIN, *VALIDATE),
               test_seasons=TEST) -> dict:  # fmt: skip
    """`games`: analysis.dataset GameRows (closing lines, for scoring only); `units_preds`:
    game_id -> M11 Prediction."""
    if report_path.exists() and not force:
        raise RuntimeError(f"test seasons already scored ({report_path}); they are scored once")
    fits = fit_design(rows, out, design, fit_seasons)
    test_rows = rows[rows.season.isin(test_seasons)]
    pred = predict_rows(test_rows, fits)
    elo = {g.game_id: p for g, p in walk_forward(games, EloModel(**ELO_M10))}
    by_id = {g.game_id: g for g in games}

    report: dict = {"design": design.__dict__, "seasons": list(test_seasons),
                    "fit_seasons": list(fit_seasons), "points": {}}
    for point in POINTS:
        p = pred[pred.point == point].set_index("game_id")
        common = [i for i in p.index
                  if i in out.index and i in units_preds and by_id[i].spread_home is not None
                  and by_id[i].total is not None and units_preds[i] is not None]  # fmt: skip
        actual_m = out.loc[common, "margin"].to_numpy()
        actual_t = out.loc[common, "total"].to_numpy()

        def mae(x, y):
            return float(np.mean(np.abs(np.asarray(x) - y)))

        m12_m, m12_t = p.loc[common, "margin"].to_numpy(), p.loc[common, "total"].to_numpy()
        close_m = [-by_id[i].spread_home for i in common]
        close_t = [by_id[i].total for i in common]
        res = {
            "games": len(common),
            "margin_mae": {"m12": mae(m12_m, actual_m),
                           "elo": mae([elo[i].home_margin for i in common], actual_m),
                           "m11": mae([units_preds[i].home_margin for i in common], actual_m),
                           "close": mae(close_m, actual_m)},
            "total_mae": {"m12": mae(m12_t, actual_t),
                          "m11": mae([units_preds[i].total for i in common], actual_t),
                          "close": mae(close_t, actual_t)},
            "calibration": calibration(p.loc[common].reset_index(), out),
        }  # fmt: skip
        bets = {"m12": [], "elo": [], "m11": []}
        for i in common:
            g = by_id[i]
            for name, (pm, pt) in (("m12", (p.at[i, "margin"], p.at[i, "total"])),
                                   ("elo", (elo[i].home_margin, None)),
                                   ("m11", (units_preds[i].home_margin, units_preds[i].total))):
                bets[name].append(bets_at_close(pm, pt, g.spread_home, g.total, g.home_score,
                                                g.away_score))  # fmt: skip
        res["bets"] = {
            f"{name} {market}": tally(f"{name} {market}", [b[market] for b in bl]).as_dict()
            for name, bl in bets.items() for market in ("spread", "total")
            if any(b[market] for b in bl)
        }  # fmt: skip
        report["points"][point] = res
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=1, default=float))
    return report


# --- replay of the live weeks ------------------------------------------------------------------


def read_live_csvs(paths: list[Path]) -> dict[tuple[str, str, str], dict]:
    """(kickoff, away, home) -> what M10 and M11 said, from live/*.csv."""
    out: dict[tuple[str, str, str], dict] = {}
    for path in paths:
        for r in csv.DictReader(path.open()):
            key = (r["kickoff_utc"][:16], r["away"], r["home"])
            d = out.setdefault(key, {})
            if r["model_version"].startswith("elo"):
                d["elo_spread"] = float(r["model_home_spread"])
                d["elo_market"] = float(r["market_home_spread"])
            else:
                d["m11_spread"] = float(r["model_home_spread"])
                d["m11_total"] = float(r["model_total"])
                d["m11_market"] = float(r["market_home_spread"])
                d["m11_market_total"] = float(r["market_total"])
    return out


def kickoff_key(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M")
