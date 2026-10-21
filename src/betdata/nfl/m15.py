"""The m15 NFL side version: its design file, its groups, and its one test-season scoring.

config/nfl_model_m15.json holds the Design fields plus m15's own keys (`m15_group_versions`:
which build of each kept M15 group; `page`: the M15 switch decision; `fit`: the frozen live fit
under config/fits/; `status`). `load_design15` ignores the extra keys when building the Design.

`final_test15` refits m15 and m13w on 2013-2022 and scores 2023-2025 once. It is the third
look at those seasons (M12 and M13 scored them before), and nothing is adjusted afterwards.
The page switch (MODEL.md "M15 evaluation rules"): validation improved AND the test margin MAE
is not worse than m13w's recorded final test at either prediction point.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import numpy as np
import pandas as pd

from betdata.config import PROJECT_ROOT
from betdata.nfl.final import bets_at_close, tally
from betdata.nfl.pipeline import TEST, TRAIN, VALIDATE
from betdata.nfl.points import POINTS
from betdata.nfl.selection import BET_THRESHOLDS, Design, fit_design, predict_rows

DESIGN15_PATH = PROJECT_ROOT / "config" / "nfl_model_m15.json"
REPORT15_PATH = PROJECT_ROOT / "reports" / "nfl" / "m15_final_test.json"
LABEL = "third look at 2023-2025 (M12 and M13 scored them before)"
# m13w's final test (MODEL.md "Final test of the weather version", Actions `weather` #2; the
# report itself stayed in the runner state): margin / total MAE per point, 855 games.
M13W_RECORDED = {"final_report": {"margin": 10.057, "total": 10.377},
                 "prekick": {"margin": 9.992, "total": 10.374}}  # fmt: skip
M13W_VALIDATION_MARGIN = 9.819


def load_design15(path: Path = DESIGN15_PATH) -> tuple[Design, dict]:
    raw = json.loads(path.read_text())
    names = {f.name for f in fields(Design)}
    return Design(**{k: v for k, v in raw.items() if k in names}), {
        k: v for k, v in raw.items() if k not in names}  # fmt: skip


def save_meta(updates: dict, path: Path = DESIGN15_PATH) -> None:
    raw = json.loads(path.read_text())
    path.write_text(json.dumps({**raw, **updates}, indent=2) + "\n")


def m15_group(name: str, version: int):
    """The build of an M15 group (the full build, or a targeted adjustment by its version)."""
    from betdata.nfl.groups15 import GROUPS15
    from betdata.nfl.select15 import _variants

    builds = [GROUPS15[name], *(g for _, g in _variants().get(name, []))]
    for g in builds:
        if g.version == version:
            return g
    raise ValueError(f"no build of {name!r} with version {version}")


def groups_of(design: Design, meta: dict) -> list:
    """Every group object the design reads: M12/M13 groups, then the M15 builds."""
    from betdata.nfl.groups import GROUPS

    versions = meta.get("m15_group_versions", {})
    return [GROUPS[n] if n in GROUPS else m15_group(n, versions[n]["version"])
            for n in design.groups]  # fmt: skip


def rows15(store, design: Design, meta: dict, seasons, refresh: bool = False) -> pd.DataFrame:
    from betdata.nfl.workflow import feature_rows, model_games

    ids = model_games(store, seasons)
    rows = None
    for grp in groups_of(design, meta):
        r = feature_rows(store, grp, ids, refresh)
        rows = r if rows is None else rows.merge(r.drop(columns=["season", "week"]),
                                                 on=["game_id", "point"])  # fmt: skip
    return rows


def _ml(pred, out, ml_closes, cfg) -> dict:
    from betdata.nfl.moneyline import bets, frame, scores, summary

    d = frame(pred, out, ml_closes)
    return {**scores(d, cfg.calibration),
            "bets": summary(bets(d, cfg.threshold_pts, cfg.calibration))}  # fmt: skip


def final_test15(rows: pd.DataFrame, out: pd.DataFrame, d15: Design, d13: Design, games_by_id,
                 ml_closes: dict, ml_cfg, path: Path = REPORT15_PATH, again: bool = False,
                 validation_margin: float | None = None) -> dict:  # fmt: skip
    """`rows`: features of both designs for train, validation and test games. `games_by_id`:
    analysis.dataset GameRows (closing lines and scores, for scoring only)."""
    if path.exists() and not again:
        raise RuntimeError(f"test seasons already scored for m15 ({path}); pass --again")
    test = rows[rows.season.isin(TEST)]
    preds = {name: predict_rows(test, fit_design(rows, out, d, (*TRAIN, *VALIDATE)))
             for name, d in (("m15", d15), ("m13w", d13))}  # fmt: skip
    report: dict = {"label": LABEL, "seasons": list(TEST), "fit_seasons": [*TRAIN, *VALIDATE],
                    "design": d15.__dict__, "m13w_recorded": M13W_RECORDED, "points": {}}
    for point in POINTS:
        res: dict = {}
        p = {k: v[v.point == point].set_index("game_id") for k, v in preds.items()}
        ids = [i for i in p["m15"].index if i in out.index]
        y_m, y_t = out.loc[ids, "margin"].to_numpy(), out.loc[ids, "total"].to_numpy()
        res["games"] = len(ids)
        res["margin_mae"] = {k: float(np.abs(v.loc[ids, "margin"] - y_m).mean())
                             for k, v in p.items()}  # fmt: skip
        res["total_mae"] = {k: float(np.abs(v.loc[ids, "total"] - y_t).mean())
                            for k, v in p.items()}  # fmt: skip
        res["margin_mae"]["m13w_recorded"] = M13W_RECORDED[point]["margin"]
        res["total_mae"]["m13w_recorded"] = M13W_RECORDED[point]["total"]
        lined = [i for i in ids if games_by_id[i].spread_home is not None
                 and games_by_id[i].total is not None]  # fmt: skip
        ym, yt = out.loc[lined, "margin"].to_numpy(), out.loc[lined, "total"].to_numpy()
        res["with_close"] = {
            "games": len(lined),
            "margin_mae": {**{k: float(np.abs(v.loc[lined, "margin"] - ym).mean())
                              for k, v in p.items()},
                           "close": float(np.abs(-np.array([games_by_id[i].spread_home
                                                            for i in lined]) - ym).mean())},
            "total_mae": {**{k: float(np.abs(v.loc[lined, "total"] - yt).mean())
                             for k, v in p.items()},
                          "close": float(np.abs(np.array([games_by_id[i].total
                                                          for i in lined]) - yt).mean())},
        }  # fmt: skip
        res["spread_bets"] = {}
        for k, v in p.items():
            results = [bets_at_close(v.at[i, "margin"], None, games_by_id[i].spread_home, None,
                                     games_by_id[i].home_score,
                                     games_by_id[i].away_score)["spread"] for i in lined]
            res["spread_bets"][k] = tally(f"{k} spread", results).as_dict()
        res["moneyline"] = {k: _ml(v.reset_index(), out, ml_closes, ml_cfg)
                            for k, v in p.items()}  # fmt: skip
        report["points"][point] = res
    report["spread_threshold"] = BET_THRESHOLDS["spread"]
    report["moneyline_threshold_pts"] = ml_cfg.threshold_pts
    report["page"] = page_switch(report, validation_margin)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1, default=float) + "\n")
    return report


def page_switch(report: dict, validation_margin: float | None) -> dict:
    """M15 Part A rule: validation improved and test margin MAE not worse at either point."""
    valid_ok = validation_margin is not None and validation_margin < M13W_VALIDATION_MARGIN
    per_point = {p: r["margin_mae"]["m15"] <= M13W_RECORDED[p]["margin"]
                 for p, r in report["points"].items()}  # fmt: skip
    return {"validation_margin": validation_margin, "m13w_validation_margin":
            M13W_VALIDATION_MARGIN, "validation_improved": valid_ok,
            "test_not_worse": per_point, "switch": bool(valid_ok and all(per_point.values()))}
