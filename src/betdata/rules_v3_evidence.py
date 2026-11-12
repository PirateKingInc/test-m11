"""Rules v3 evidence (owner decision 2026-11-12, MODEL.md "Rules v3"; `reports/nba/rules_v3.md`).
REPORT ONLY, labelled hindsight: nothing here touches a card, pick, rule, alert, push or ledger.

1. The live ledger split that led to Rules v3, recomputed: live F picks from Oct 29 (rebounds and
   assists), one per player-game-market-side (the first created), flat 1u, graded only; each side
   split by OG Ranking v2's median rule (the stored nbap15 median strictly on the bet's side).
2. The 2023-24 to 2025-26 seasons with the threes_backtest pipeline (betdata/nba/props/
   threes_bt.py: nbap15's final-test fit, F-style candidates at the opening line, Ranking v2's
   price and median rules, one pick per player-game-market): rebounds by side; and the top-N
   card per day of assists (Rules v2 assist filters) alone vs assists + rebound overs.
"""

from __future__ import annotations

import json
from pathlib import Path

LIVE_FROM = "2026-10-29"
REPORT_MD = Path("reports/nba/rules_v3.md")
REPORT_JSON = Path("reports/nba/rules_v3.json")


def _rec(rows: list[dict]) -> dict:
    from betdata import official as O

    r = O.record(rows)
    d = r.wins + r.losses
    return {"w": r.wins, "l": r.losses, "p": r.pushes, "graded": r.graded,
            "win": None if not d else round(r.wins / d, 4), "units": round(r.units, 2)}


def live_split(rows: list[dict]) -> dict:
    """{market: {side: {"kept": rec, "removed": rec, "all": rec}}} (module docstring, 1)."""
    f = [r for r in rows if r.get("signal_name") == "F" and r["day"] >= LIVE_FROM
         and r.get("market") in ("rebounds", "assists")]  # fmt: skip
    first: dict = {}
    for r in sorted(f, key=lambda r: r.get("created_at", "")):
        first.setdefault((r["game_id"], (r.get("player") or "").lower(), r["market"],
                          r["side"]), r)  # fmt: skip
    u = list(first.values())
    out: dict = {}
    for mk in ("rebounds", "assists"):
        for sd in ("over", "under"):
            g = [r for r in u if r["market"] == mk and r["side"] == sd]

            def keeps(r, sd=sd):
                m, ln = r.get("proj_median"), r.get("line")
                if m in ("", None) or ln in ("", None):
                    return None
                return float(m) > float(ln) if sd == "over" else float(m) < float(ln)

            out.setdefault(mk, {})[sd] = {
                "kept": _rec([r for r in g if keeps(r) is True]),
                "removed": _rec([r for r in g if keeps(r) is False]),
                "all": _rec(g)}  # fmt: skip
    return out


def old_seasons(pools: dict, tm: dict | None) -> dict:
    """Module docstring, 2: per prediction point."""
    import numpy as np
    import pandas as pd

    from betdata.nba.props import threes_bt as T

    res: dict = {}
    for pt in T.POINTS:
        if ("rebounds", pt) not in pools or ("assists", pt) not in pools:
            continue
        rc = T.candidates(pools[("rebounds", pt)], "rebounds")
        rc = rc[rc.season.isin(T.TEST_SEASONS)]
        med = np.where(rc.side == "over", rc["median"] > rc.line, rc["median"] < rc.line)
        rc = rc.assign(dec=[1 + (x / 100 if x > 0 else 100 / -x) for x in rc.price])
        cand = {sd: {"median_kept": T.rec(rc[(rc.side == sd) & med]),
                     "median_removed": T.rec(rc[(rc.side == sd) & ~med])}
                for sd in ("over", "under")}  # fmt: skip
        rb = T.ranking_v2(T.candidates(pools[("rebounds", pt)], "rebounds"))
        rb = rb[rb.season.isin(T.TEST_SEASONS)]
        a = T.ranking_v2(T.candidates(pools[("assists", pt)], "assists"))
        a = a[a.season.isin(T.TEST_SEASONS)].copy()
        if tm is not None:
            a["tm"] = [tm.get(("assists", pt, g, p)) for g, p in zip(a.game_id, a.player_id,
                                                                      strict=True)]  # fmt: skip
        keep = ~((a.side == "under") & (a.line >= T.UNDER_LINE - 1e-9))
        if "tm" in a:
            keep &= ~((a.side == "under") & (a.tm.fillna(0) >= T.TEAMMATES_MAX))
        a = a[keep]
        days = pools[("assists", pt)].loc[lambda x: x.season.isin(T.TEST_SEASONS)].day.nunique()
        ro = rb[rb.side == "over"]
        r = {"candidates_by_side": cand,
             "ranking_v2_by_side": {sd: {"all": T.rec(rb[rb.side == sd]), "by_season": {
                 int(s): T.rec(g) for s, g in rb[rb.side == sd].groupby("season")}}
                 for sd in ("over", "under")}}  # fmt: skip
        for n in (3, 5):
            alone = T.top_per_day(a, n)
            both = T.top_per_day(pd.concat([a, ro]), n)
            r[f"card_top{n}"] = {
                "assists_alone": T.rec(alone, days),
                "assists_plus_rebound_overs": T.rec(both, days),
                "rebound_overs_share": None if not len(both) else
                round(float((both.market == "rebounds").mean()), 3),
                "by_season": {int(s): {"assists_alone": T.rec(alone[alone.season == s]),
                                       "assists_plus_rebound_overs":
                                           T.rec(both[both.season == s])}
                              for s in T.TEST_SEASONS}}  # fmt: skip
        res[pt] = r
    return res


def agreement(live: dict, old: dict) -> str:
    """Whether the old seasons agree with the live split: rebound overs (Ranking v2) beat rebound
    unders on win rate and are profitable, and adding them to the assist card does not lower its
    units per pick (top 5)."""
    lo = live["rebounds"]["over"]["kept"]
    lu = live["rebounds"]["under"]["all"]
    msgs, verdicts = [], []
    for pt, r in old.items():
        o, u = r["ranking_v2_by_side"]["over"]["all"], r["ranking_v2_by_side"]["under"]["all"]
        c = r["card_top5"]
        ok_side = (o["win"] or 0) > (u["win"] or 0) and o["units"] > 0
        ok_card = (c["assists_plus_rebound_overs"]["roi"] or 0) >= (c["assists_alone"]["roi"]
                                                                     or 0)  # fmt: skip
        msgs.append(f"{pt}: overs {T_wl(o)} ({_pc(o['win'])}, {o['units']:+.1f}u) vs unders "
                    f"{T_wl(u)} ({_pc(u['win'])}, {u['units']:+.1f}u) — "
                    + ("**agrees** on the side split" if ok_side else
                       "**does not agree** on the side split")
                    + "; the top-5 card with rebound overs "
                    + ("does at least as well per pick as assists alone" if ok_card else
                       "does worse per pick than assists alone"))
        verdicts.append(ok_side)
    head = (f"Live (hindsight): rebound overs kept by the median rule {lo['w']}-{lo['l']} "
            f"({_pc(lo['win'])}, {lo['units']:+.1f}u) vs all rebound unders {lu['w']}-{lu['l']} "
            f"({_pc(lu['win'])}, {lu['units']:+.1f}u).")
    plain = ("**The old seasons agree with the live split.**" if verdicts and all(verdicts) else
             "**The old seasons do not agree with the live split**: on 2023-2025 rebound "
             "unders, not overs, were the profitable side under Ranking v2. Rules v3 rests on "
             "the live hindsight evidence alone; the guardrail and the review decide."
             if verdicts and not any(verdicts) else
             "**Mixed:** the prediction points disagree." if verdicts else
             "No old-season data.")  # fmt: skip
    return "\n".join([plain, "", head, *[f"- {m}" for m in msgs]])


def T_wl(r: dict) -> str:  # noqa: N802 - threes_bt's W-L format
    return f"{r['w']}-{r['l']}" + (f"-{r['p']}" if r.get("p") else "")


def _pc(x) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def _live_table(live: dict) -> list[str]:
    out = ["| Market · side | Median rule keeps | Median rule removes | All |",
           "|---|---|---|---|"]
    for mk in ("rebounds", "assists"):
        for sd in ("over", "under"):
            x = live[mk][sd]
            out.append(f"| {mk} · {sd} | " + " | ".join(
                f"{v['w']}-{v['l']} ({_pc(v['win'])}, {v['units']:+.1f}u)"
                for v in (x["kept"], x["removed"], x["all"])) + " |")
    return out


def _old_tables(old: dict) -> list[str]:
    out = []
    for pt, r in old.items():
        out += [f"### Prediction point: {pt}", "",
                "| Rebounds | Candidates, median keeps | Candidates, median removes | "
                "Ranking v2 pool | 2023-24 | 2024-25 | 2025-26 |", "|---|---|---|---|---|---|---|"]
        for sd in ("over", "under"):
            c, rv = r["candidates_by_side"][sd], r["ranking_v2_by_side"][sd]
            cells = [c["median_kept"], c["median_removed"], rv["all"],
                     *[rv["by_season"].get(s, {"w": 0, "l": 0, "win": None, "units": 0.0})
                       for s in (2023, 2024, 2025)]]  # fmt: skip
            out.append(f"| {sd} | " + " | ".join(
                f"{T_wl(v)} ({_pc(v['win'])}, {v['units']:+.1f}u)" for v in cells) + " |")
        out += ["", "| Card per day (Ranking v2) | Assists alone | Assists + rebound overs | "
                "Rebound overs share |", "|---|---|---|---|"]
        for n in (3, 5):
            c = r[f"card_top{n}"]
            a, b = c["assists_alone"], c["assists_plus_rebound_overs"]
            out.append(f"| Top {n} | {T_wl(a)}, {a['units']:+.1f}u, ROI {_pc(a['roi'])}, "
                       f"{a['per_day']}/day | {T_wl(b)}, {b['units']:+.1f}u, ROI {_pc(b['roi'])}"
                       f", {b['per_day']}/day | {_pc(c['rebound_overs_share'])} |")
        out.append("")
    return out


def report(live: dict, old: dict, generated: str) -> str:
    md = [f"# Rules v3 evidence ({generated})", "",
          "Owner decision 2026-11-12 (MODEL.md \"Rules v3\"): from the Nov 13 card, rebound "
          "overs are eligible for the official OG card again; rebound unders stay out. "
          "**Report only, hindsight evidence**: the live split below is what led to the "
          "decision (found after looking), so it cannot confirm itself; the old seasons are "
          "the out-of-sample check. Nothing here changes a card.", "",
          f"## Live F picks since {LIVE_FROM[5:]} (one per player-game-side, flat 1u, graded)",
          "", *_live_table(live), "",
          "## 2023-24 to 2025-26 (threes_backtest pipeline, Ranking v2 rules)", "",
          "nbap15's final-test fit (no refit), F-style candidates at the opening line (6 pp), "
          "Ranking v2's price -150..+130 and median rules, one pick per player-game-market; "
          "assists carry Rules v2's assist filters (no unders on 5.5+ lines; no unders with 2+ "
          "key teammates out where the injury report is known). One book per line, so F's "
          "consensus check cannot apply.", "", *_old_tables(old),
          "## Do the old seasons agree?", "", agreement(live, old), ""]
    return "\n".join(md) + "\n"


def build(root: Path, pools: dict, tm: dict | None, generated: str) -> tuple[str, dict]:
    from betdata import official as O

    live = live_split(O.dataset(root / "ledger"))
    old = old_seasons(pools, tm)
    md = report(live, old, generated)
    (root / REPORT_MD).write_text(md)
    data = {"generated": generated, "live": live, "old_seasons": old}
    (root / REPORT_JSON).write_text(json.dumps(data, indent=1, default=str) + "\n")
    return md, data


def main() -> None:  # pragma: no cover - research run on the history database
    """`python -m betdata.rules_v3_evidence`: the pools from threes_bt.load on the history
    database (or the pickled pools of the threes research run, RULES_V3_POOLS / RULES_V3_TM)."""
    import os
    import pickle
    from datetime import UTC, datetime

    pools_pkl, tm_pkl = os.environ.get("RULES_V3_POOLS"), os.environ.get("RULES_V3_TM")
    if pools_pkl:
        with open(pools_pkl, "rb") as fh:
            pools = pickle.load(fh)["pools"]  # noqa: S301 - our own research output
        tm = None
        if tm_pkl:
            with open(tm_pkl, "rb") as fh:
                tm = pickle.load(fh)  # noqa: S301
    else:
        from betdata.config import get_settings
        from betdata.db.engine import make_engine
        from betdata.nba.props import threes_bt as T

        eng = make_engine(get_settings().db_url)
        with eng.connect() as c:
            pools = T.load(c)["pools"]
        tm = None
    md, _ = build(Path("."), pools, tm, datetime.now(UTC).date().isoformat())
    print(md)


if __name__ == "__main__":
    main()
