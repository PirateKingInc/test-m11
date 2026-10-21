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


RAPM_CACHE = Path("data/derived/nba_rows15_rapm.pkl")


def rapm_rows(store, gids, rebuild: bool = False):
    """The RAPM group's rows (weekly snapshots from the stints table), cached separately."""
    from betdata.nba.cli import _engine
    from betdata.nba.rapm import load_stints, snapshot_times, snapshots

    if RAPM_CACHE.exists() and not rebuild:
        return pickle.loads(RAPM_CACHE.read_bytes())
    _, engine = _engine()
    with engine.connect() as conn:
        stints = load_stints(conn)
    snaps = snapshots(stints, snapshot_times(store))
    rows = build(store.with_params(rapm=snaps), [GROUPS15["rapm"]], gids)
    RAPM_CACHE.parent.mkdir(parents=True, exist_ok=True)
    RAPM_CACHE.write_bytes(pickle.dumps((rows, len(snaps))))
    return rows, len(snaps)


def rows15(rebuild: bool = False, groups: list[str] | None = None):
    """(store with the load table, outcomes, M14 rows joined with the M15 groups' rows)."""
    from betdata.nba.cli import _rows

    store, out, rows, _ = _rows()
    names = [n for n in (groups or ORDER15) if n != "rapm"]
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
    rr, _ = rapm_rows(store, sorted(rows.game_id.unique()), rebuild)
    keep = [c for c in rr.columns if c not in ("season",)]
    joined = joined.merge(rr[keep], on=["game_id", "point"], how="left")
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


DESIGN15 = Path(__file__).resolve().parents[3] / "config" / "nba15_model.json"
DROP_ZERO_ABLATION = True  # a kept group whose removal costs nothing is dropped (Occam)


def design15() -> dict:
    """The nba15 design from the selection report: kept groups (minus any whose ablation shows
    no loss), the lambdas re-chosen on validation, M14's thresholds and calibration."""
    from betdata.nba.live import Design

    rep = json.loads((REPORTS / "nba15_selection.json").read_text())
    final = rep["final"]["margin"]
    zero = {a["without"] for a in rep["ablation"] if a["margin"] <= final + 1e-9}
    groups = [g for g in rep["kept"] if not (DROP_ZERO_ABLATION and g in zero)]
    m14 = Design.load()
    return {"groups": groups, "dropped_by_ablation": sorted(zero), "lam_m": None,
            "lam_t": None, "spread_threshold": m14.spread_threshold,
            "ml_threshold": m14.ml_threshold, "blend_weight": m14.blend_weight,
            "calibration": m14.calibration, "fit": "", "page": False}  # fmt: skip


def final_test15(again: bool = False) -> dict:
    """The nba15 version on 2023-24 to 2025-26: the SECOND look at these seasons (M14 scored
    them first). Scored once; nothing is adjusted afterwards. Compared with the frozen M14
    version's own final test and with the closing line."""
    import numpy as np

    from betdata.nba.pipeline import TEST, columns, evaluate
    from betdata.nba.selection import fits_for, market_frame, predictions

    path = REPORTS / "nba15_final_test.json"
    if path.exists() and not again:
        raise SystemExit(f"{path} exists: scored once (--again only to reproduce)")
    d = design15()
    store, out, rows, _ = rows15()
    m, t = columns(rows, d["groups"])
    ev = evaluate(rows, out, m, t)
    d["lam_m"], d["lam_t"] = ev.lam_m, ev.lam_t
    fits = fits_for(rows, out, m, t, TRAIN + VALIDATE, ev.lam_m, ev.lam_t)
    pred = predictions(rows, out, fits, TEST).join(market_frame(store, rows.game_id.unique()),
                                                   on="game_id")  # fmt: skip
    m14 = json.loads((REPORTS / "final_test.json").read_text())
    res = {"look": "second look at 2023-24 to 2025-26 (M14 scored them first)",
           "design": d, "validation_margin": ev.margin, "points": {}}  # fmt: skip
    for p in ("morning", "pretip", "all"):
        s = pred if p == "all" else pred[pred.point == p]
        c = s.close_spread_home.notna()
        res["points"][p] = {
            "margin_mae": float(np.abs(s.pred - s.margin).mean()),
            "total_mae": float(np.abs(s.pred_total - s.total).mean()),
            "close_margin_mae": float(np.abs(-s.close_spread_home[c] - s.margin[c]).mean()),
            "m14_margin_mae": m14[p]["margin_mae"]}  # fmt: skip
    val14 = json.loads((REPORTS / "nba15_selection.json").read_text())["steps"][0]["margin"]
    better_valid = ev.margin < val14
    not_worse = all(res["points"][p]["margin_mae"] <= res["points"][p]["m14_margin_mae"] + 1e-9
                    for p in ("morning", "pretip"))  # fmt: skip
    res["page_switch"] = bool(better_valid and not_worse)
    res["rule"] = ("switch only if validation improved AND the test margin MAE is not worse at "
                   "either point than the M14 version's")
    d["page"] = res["page_switch"]
    path.write_text(json.dumps(res, indent=1, default=float) + "\n")
    DESIGN15.write_text(json.dumps(d, indent=2) + "\n")
    return res


def freeze15() -> Path:
    """Fit nba15 on every completed season (2015-2025) for the live runner (no hourly refit).
    Written to its own file; the frozen M14 fit is never touched."""
    from betdata.config import PROJECT_ROOT
    from betdata.nba.live import Design, load_fit
    from betdata.nba.pipeline import columns
    from betdata.nba.selection import fits_for

    d = json.loads(DESIGN15.read_text())
    _, out, rows, load = rows15()
    seasons = tuple(range(2015, 2026))
    m, t = columns(rows, d["groups"])
    fits = fits_for(rows, out, m, t, seasons, d["lam_m"], d["lam_t"])
    raw = {"seasons": list(seasons), "design": d, "play_prob": load_fit(Design.load())[1],
           "load_sit": load,
           "fits": {p: {"margin": f.margin.to_dict(), "total": f.total.to_dict(),
                        "sigma": f.sigma} for p, f in fits.items()}}  # fmt: skip
    target = PROJECT_ROOT / "config" / "fits" / "nba15_fit_2015_2025.json"
    target.write_text(json.dumps(raw, indent=1, sort_keys=True) + "\n")
    d["fit"] = str(target.relative_to(PROJECT_ROOT))
    DESIGN15.write_text(json.dumps(d, indent=2) + "\n")
    return target
