"""M15 NFL side selection (`betdata nfl select15`), validation seasons only.

Starts from the design on the page (m13w: config/nfl_model_wx.json, M12 + weather in the
margin model), fits on M12's training seasons 2013-2019 and scores 2020-2022, exactly like
`betdata nfl evaluate`: the five M15 groups are added one at a time in MODEL.md order; a group's
margin half is kept if it lowers validation margin MAE, its total half if it lowers total MAE
(M12's rule; ridge penalty chosen on validation from M12's grid). A dropped full build gets up
to three targeted adjustments (ADJUSTMENTS), each recorded. Then the ablation: validation MAE
with each kept M15 group removed. Test seasons (2023-2025) are never built or scored here.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from betdata.config import PROJECT_ROOT
from betdata.nfl.groups import columns
from betdata.nfl.groups15 import GROUPS15, ORDER15
from betdata.nfl.pipeline import TRAIN, VALIDATE, evaluate
from betdata.nfl.selection import Design
from betdata.nfl.weather import WX_DESIGN_PATH

M15_DESIGN_PATH = PROJECT_ROOT / "config" / "nfl_model_m15.json"
REPORT_PATH = PROJECT_ROOT / "reports" / "nfl" / "m15_selection.json"


def _variants() -> dict[str, list[tuple[str, object]]]:
    """Targeted adjustments per group (same feature names, own cache version)."""
    from betdata.nfl.features import m15_depth, m15_lines, m15_pen, m15_sitluck, m15_st

    return {
        "lines": m15_lines.ADJUSTMENTS,
        "depth": m15_depth.ADJUSTMENTS,
        "st": m15_st.ADJUSTMENTS,
        "sitluck": m15_sitluck.ADJUSTMENTS,
        "pen": m15_pen.ADJUSTMENTS,
    }


def _ev(rows, out, m, t, lam=None):
    if lam is None:
        return evaluate(rows, out, m, t, TRAIN, VALIDATE)
    return evaluate(rows, out, m, t, TRAIN, VALIDATE, lambdas=(lam[0],), lambdas_t=(lam[1],))


def _rec(label, ev) -> dict:
    return {"step": label, "margin_mae": round(ev.margin, 4), "total_mae": round(ev.total, 4),
            "lam_margin": ev.lam_m, "lam_total": ev.lam_t,
            "by_point": {p: {"margin": round(v[0], 4), "total": round(v[1], 4), "games": v[2]}
                         for p, v in ev.valid.items()}}  # fmt: skip


def select15(base_rows, out, group_rows, start: Design | None = None, log=print) -> dict:
    """base_rows: feature rows of the start design's groups; group_rows(group) -> that M15
    group's rows (same game ids). Returns the report; nothing is written here."""
    start = start or Design.load(WX_DESIGN_PATH)
    rows = base_rows
    kept_m, kept_t = start.columns(rows.columns)
    groups_m, groups_t = list(start.margin_groups), list(start.total_groups)
    ev0 = _ev(rows, out, kept_m, kept_t)
    best_m, best_t = ev0.margin, ev0.total
    steps = [{**_rec("start (m13w design)", ev0), "decision": "start"}]
    log(f"{'start (m13w design)':34} {ev0.margin:8.3f} {ev0.total:8.3f}")
    variants = _variants()
    versions: dict[str, dict] = {}
    for name in ORDER15:
        tries = [("full build", GROUPS15[name]), *variants.get(name, [])[:3]]
        for label, grp in tries:
            r = group_rows(grp)
            cand = rows.merge(r.drop(columns=["season", "week"]), on=["game_id", "point"])
            m, t = columns(cand.columns, name)
            ev = _ev(cand, out, kept_m + m, kept_t + t)
            keep_m, keep_t = ev.margin < best_m, ev.total < best_t
            dec = f"{'KEEP' if keep_m else 'drop'} / {'KEEP' if keep_t else 'drop'}"
            log(f"{'+ ' + name + ' (' + label + ')':34} {ev.margin:8.3f} {ev.total:8.3f}  "
                f"{ev.lam_m:g}/{ev.lam_t:g}  {dec}")  # fmt: skip
            steps.append({**_rec(f"+ {name} ({label})", ev), "group": name, "variant": label,
                          "decision_margin": "KEEP" if keep_m else "drop",
                          "decision_total": "KEEP" if keep_t else "drop"})  # fmt: skip
            if not (keep_m or keep_t):
                continue  # next targeted adjustment, if any is left
            rows = cand.drop(columns=[c for c in m if not keep_m] +
                             [c for c in t if not keep_t])  # fmt: skip
            if keep_m:
                kept_m, best_m = kept_m + m, ev.margin
                groups_m.append(name)
            if keep_t:
                kept_t, best_t = kept_t + t, ev.total
                groups_t.append(name)
            versions[name] = {"version": grp.version, "variant": label}
            break  # a kept build ends the group (M12: adjustments only while it doesn't help)
    final = _ev(rows, out, kept_m, kept_t)
    lam = (final.lam_m, final.lam_t)
    ablation = [{"without": "-", "margin_mae": round(final.margin, 4),
                 "total_mae": round(final.total, 4)}]  # fmt: skip
    log(f"final: margin {groups_m} total {groups_t}: {final.margin:.3f} / {final.total:.3f} "
        f"(penalties {final.lam_m:g} / {final.lam_t:g})")
    log("ablation (validation MAE, each kept M15 group removed):")
    for g in [x for x in ORDER15 if x in groups_m or x in groups_t]:
        m = [c for c in kept_m if not c.startswith(f"m_{g}_")]
        t = [c for c in kept_t if not c.startswith(f"t_{g}_")]
        e = _ev(rows, out, m, t, lam)
        ablation.append({"without": g, "margin_mae": round(e.margin, 4),
                         "total_mae": round(e.total, 4),
                         "delta_margin": round(e.margin - final.margin, 4),
                         "delta_total": round(e.total - final.total, 4)})  # fmt: skip
        log(f"  without {g:10} {e.margin:.3f} / {e.total:.3f}  "
            f"({e.margin - final.margin:+.3f} / {e.total - final.total:+.3f})")
    design = replace(start, margin_groups=groups_m, total_groups=groups_t,
                     lam_margin=final.lam_m, lam_total=final.lam_t)  # fmt: skip
    return {"train": list(TRAIN), "validate": list(VALIDATE), "start": start.__dict__,
            "steps": steps, "final": _rec("final", final), "design": design.__dict__,
            "group_versions": versions, "ablation": ablation}  # fmt: skip


def write(report: dict, info: dict, path: Path = REPORT_PATH,
          design_path: Path = M15_DESIGN_PATH) -> None:  # fmt: skip
    """The report, and the draft m15 design: Design fields plus `m15_group_versions` (which
    build of each kept M15 group; a version above 100 is a targeted adjustment)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**report, "inputs": info}, indent=1, default=float) + "\n")
    draft = {**report["design"], "m15_group_versions": report["group_versions"],
             "status": "draft: chosen on validation 2020-2022 only; no test, not used live"}
    design_path.write_text(json.dumps(draft, indent=2) + "\n")
