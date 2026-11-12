"""The Action card review (owner decision 2026-11-12, MODEL.md "Action card"): at 100 graded
Action picks, `reports/nba/action_review.md` and one ntfy line; the weekly report's Action
section; and the hindsight replay of Oct 31 - Nov 12 (labelled, never in the live record).
Report only: nothing changes automatically; the owner decides.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

MILESTONE = "action-card"
REVIEW_N = 100
REPORT = Path("reports") / "nba" / "action_review.md"
REPLAY = Path("reports") / "nba" / "action_replay.json"
STAKE = 0.25


def rows_of(out: Path) -> list[dict]:
    from betdata import cards as C
    from betdata import official as O

    return C.view(O.dataset(out), C.ACTION)


def _u(x: float) -> str:
    return f"{x:+.2f}u"


def _roi(rec) -> str:
    return "–" if rec.roi is None else f"{100 * rec.roi:+.1f}%"


def summary(rows: list[dict]) -> list[str]:
    """Record, units, ROI; by reason tag; by market (each sums to the whole)."""
    from betdata import action as AC
    from betdata import official as O

    rec = O.record(rows, O.official_stake)
    out = [f"- Record: {rec.wlp}, {_u(rec.units)} at 0.25u, ROI {_roi(rec)} "
           f"({rec.graded} graded; graded picks only)"]
    for title, by in (("By reason", "reason"), ("By market", "market")):
        parts = AC.breakdown(rows, by)
        out.append(f"- {title}: " + ("no graded picks yet" if not parts else " · ".join(
            f"{k} {r.wlp}, {_u(r.units)}" for k, r in parts.items())))
    return out


def stacks(rows: list[dict]) -> list[str]:
    """Same-player stacks: nights with 2+ Action picks on one player, and how often they all
    won or all lost (graded picks only)."""
    from betdata import official as O

    by: dict = {}
    for r in O.graded(rows):
        by.setdefault((r["day"], (r["player"] or "").lower()), []).append(r["result"])
    st = [v for v in by.values() if len(v) >= 2]
    if not st:
        return ["- Same-player stacks: none yet"]
    allw = sum(all(x == "win" for x in v) for v in st)
    alll = sum(all(x == "loss" for x in v) for v in st)
    return [f"- Same-player stacks: {len(st)} (all won {allw}, all lost {alll}, split "
            f"{len(st) - allw - alll})"]


def vs_og(rows: list[dict], all_rows: list[dict]) -> list[str]:
    from betdata import cards as C
    from betdata import official as O

    days = {r["day"] for r in rows}
    og = [r for r in C.view(all_rows, C.OG) if r["day"] in days]
    a, b = O.record(rows, O.official_stake), O.record(og, O.official_stake)
    return [f"- Same days ({len(days)}): Action {a.wlp}, {_u(a.units)} (0.25u) vs official OG "
            f"{b.wlp}, {_u(b.units)} (0.5u)"]


def filters_compare(all_rows: list[dict]) -> list[str]:
    """The Filters table's rows for the same reasons (every removed pick, flat 1u)."""
    from betdata import eligibility as EL

    try:
        from betdata import og_ranking as OR

        t = EL.filters_table(all_rows) + OR.filters_table(all_rows)
    except Exception:  # noqa: BLE001
        return []
    og = [r for r in t if r["card"] == "OG"]
    if not og:
        return ["- Filters table (OG): no removed picks graded yet"]
    return ["- Filters table, OG, every removed pick (flat 1u): " + " · ".join(
        f"{r['reason']} {r['wlp']}, {r['units']:+.2f}u" for r in og)]


def replay(root: Path) -> list[str]:
    """The hindsight replay (runner, real databases): labelled, never in the live record."""

    path = root / REPLAY
    if not path.exists():
        return ["Replay data not found."]
    data = json.loads(path.read_text())
    rows = []
    for d, x in sorted(data.items()):
        for p in x["picks"]:
            rows.append({"day": d, "result": p.get("result", ""), "profit": p.get("profit", ""),
                         "card_stake": str(STAKE), "odds": str(p["price"]),
                         "clv_consensus": "", "action_reason": p["reason"],
                         "market": p["market"], "player": p.get("player") or ""})
    per = ", ".join(f"{d[5:]}: {len(x['picks'])}" for d, x in sorted(data.items()))
    return ["**Hindsight replay, not live results; never counted in the Action record.** Each "
            "OG card day Oct 31 - Nov 12 (Nov 5 excluded) replayed on the runner at its stored "
            "selection time with today's Action rule, excluding that day's actual official OG "
            "card (EV-ranked before Nov 12). Before Nov 12 \"ranked below top 5\" means the pick "
            "passed every Ranking v2 rule but was not on that day's EV-ranked card.", "",
            *summary(rows), *stacks(rows), "",
            f"Picks per day: {per}", ""]


def report(root: Path, now: datetime) -> str:
    from betdata import official as O

    out = root / "ledger"
    all_rows = O.dataset(out)
    rows = rows_of(out)
    md = [f"# Action card review ({now.date().isoformat()})", "",
          "Owner decision 2026-11-12 (MODEL.md \"Action card\"): the best picks OG did not make "
          "official, 3 a day at 0.25u, never official. Report only: if Action picks win, OG's "
          "rules may be too strict; if they lose, the rules earn their keep. The owner "
          "decides; nothing changes automatically.", "",
          f"## Live record (from Nov 13; review at {REVIEW_N} graded)", "", *summary(rows),
          *filters_compare(all_rows), *stacks(rows), *vs_og(rows, all_rows), "",
          "## Hindsight replay (Oct 31 - Nov 12)", "", *replay(root)]  # fmt: skip
    return "\n".join(md) + "\n"


def write(root: Path, now: datetime) -> Path:
    path = root / REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report(root, now))
    return path


def milestone(out: Path, now: datetime) -> list[dict]:
    """Once, at REVIEW_N graded Action picks."""
    from betdata import official as O
    from betdata.ledger import _append_milestones, _iso, read_rows

    if any(r["signal_name"] == MILESTONE for r in read_rows(out / "milestones.csv")):
        return []
    if O.record(rows_of(out), O.official_stake).graded < REVIEW_N:
        return []
    new = [{"signal_name": MILESTONE, "graded": str(REVIEW_N), "reached_at": _iso(now)}]
    _append_milestones(out / "milestones.csv", new)
    return new


def ntfy_line(out: Path) -> str:
    from betdata import official as O

    rec = O.record(rows_of(out), O.official_stake)
    return (f"Action review ready: {rec.wlp}, {rec.units:+.2f}u over {rec.graded} graded Action "
            f"picks — see {REPORT.as_posix()} (not official; owner decides)")


def weekly(root: Path) -> list[str]:
    """The weekly report's Action section."""
    from betdata import official as O

    out = root / "ledger"
    rows = rows_of(out)
    return ["## Action picks (not official)", "", *summary(rows), *stacks(rows),
            *vs_og(rows, O.dataset(out)), "", f"Review at {REVIEW_N} graded: "
            f"{REPORT.as_posix()}."]
