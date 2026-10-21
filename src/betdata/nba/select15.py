"""M15 NBA side version `nba15`: the M14 frozen design plus the M15 groups added one at a time
on validation (MODEL.md, "M15 evaluation rules"). Test seasons are scored once, later, as the
second look; nothing here reads them."""

from __future__ import annotations

import json
import logging
import pickle
from pathlib import Path

import pandas as pd

from betdata.nba.features15 import GROUPS15, ORDER15, fit_load
from betdata.nba.pipeline import TRAIN, VALIDATE, build, columns, evaluate

log = logging.getLogger(__name__)
CACHE = Path("data/derived/nba_rows15.pkl")
REPORTS = Path("reports/nba")


def rows15(rebuild: bool = False, groups: list[str] | None = None):
    """(store with the load table, outcomes, M14 rows joined with the M15 groups' rows)."""
    from betdata.nba.cli import _rows

    store, out, rows, _ = _rows()
    names = groups or ORDER15
    if CACHE.exists() and not rebuild:
        extra, load = pickle.loads(CACHE.read_bytes())
    else:
        load = fit_load(store, TRAIN)
        store = store.with_params(load_sit=load)
        gids = sorted(rows.game_id.unique())
        extra = build(store, [GROUPS15[n] for n in names], gids)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_bytes(pickle.dumps((extra, load)))
    store = store.with_params(load_sit=load)
    keep = [c for c in extra.columns if c not in ("season",)]
    joined = rows.merge(extra[keep], on=["game_id", "point"], how="left")
    return store, out, joined, load


def select15(rebuild: bool = False) -> dict:
    from betdata.nba.live import Design

    store, out, rows, load = rows15(rebuild)
    base_groups = list(Design.load().groups)
    m, t = columns(rows, base_groups)
    best = evaluate(rows, out, m, t)
    steps = [{"step": "M14 frozen design", "groups": base_groups, "margin": best.margin,
              "total": best.total, "lam": [best.lam_m, best.lam_t], "kept": True}]  # fmt: skip
    kept = list(base_groups)
    for grp in ORDER15:
        m, t = columns(rows, [*kept, grp])
        ev = evaluate(rows, out, m, t)
        better = ev.margin < best.margin
        steps.append({"step": f"+{grp}", "margin": ev.margin, "total": ev.total,
                      "lam": [ev.lam_m, ev.lam_t], "kept": bool(better),
                      "by_point": ev.valid})  # fmt: skip
        if better:
            kept.append(grp)
            best = ev
    ablation = []
    for grp in [g for g in kept if g in GROUPS15]:
        m, t = columns(rows, [g for g in kept if g != grp])
        ev = evaluate(rows, out, m, t)
        ablation.append({"without": grp, "margin": ev.margin, "total": ev.total})
    report = {"validate": list(VALIDATE), "kept": kept, "new_kept": [g for g in kept
                                                                   if g in GROUPS15],
              "final": {"margin": best.margin, "total": best.total,
                        "lam": [best.lam_m, best.lam_t]},
              "steps": steps, "ablation": ablation,
              "load_table": {k: v for k, v in load.items()}}  # fmt: skip
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "nba15_selection.json").write_text(json.dumps(report, indent=1, default=float))
    return report


__all__ = ["pd", "rows15", "select15"]
