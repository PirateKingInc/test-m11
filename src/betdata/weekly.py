"""The weekly report (owner request 2026-10-30): on Mondays, after the morning hourly run,
`reports/weekly/YYYY-MM-DD.md` with:
- per-signal and all-time records, with CLV and 95% ranges (vs the consensus close and,
  where one exists, Pinnacle's close);
- the C signals split by edge source (news vs book disagreement);
- system health: failed hourly runs and poller jobs, quota use.

The hourly job commits the report and pushes a 6-line ntfy summary (with the official goal
line). It also pushes an extra ntfy alert when a signal reaches 60 or 200 graded picks
(ledger milestones).
Reporting only: nothing here changes a rule, a threshold or a model.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import Connection, func, select

from betdata.alerts.rules import EASTERN
from betdata.ledger import LEGACY, TOO_FEW, Record, counted, joined

log = logging.getLogger(__name__)
NTFY = "https://ntfy.sh/"
PAGE = "https://piratekinginc.github.io/test-m11/"


def report_path(root: Path, now: datetime) -> Path:
    return root / "reports" / "weekly" / f"{now.astimezone(EASTERN).date().isoformat()}.md"


def due(root: Path, now: datetime) -> bool:
    """Mondays from the 7am ET morning run on, once."""
    t = now.astimezone(EASTERN)
    return t.weekday() == 0 and t.hour >= 7 and not report_path(root, now).exists()


def _pp(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:+.2f} pp"


def _rng(lo, hi) -> str:
    return "–" if lo is None else f"{100 * lo:+.2f} to {100 * hi:+.2f}"


def record_line(name: str, r: Record) -> str:
    n, mu, lo, hi = r.clv_stats()
    pn, pmu, plo, phi = r.clv_stats(pin=True)
    roi = "–" if r.roi is None else f"{100 * r.roi:+.1f}%"
    judge = "too few to judge" if r.graded < TOO_FEW else ""
    return (f"| {name} | {r.picks} | {r.wins}-{r.losses}-{r.pushes} ({r.voids} void) | "
            f"{r.units:+.2f} | {roi} | {_pp(mu)} ({_rng(lo, hi)}), n={n} | "
            f"{_pp(pmu)} ({_rng(plo, phi)}), n={pn} | {judge} |")


HEAD = ("| | Picks | W-L-P | Units | ROI | CLV vs consensus close (95%) | CLV vs Pinnacle close "
        "(95%) | |\n|---|---|---|---|---|---|---|---|")


def health(main: Connection | None, poller: Connection | None, since: datetime) -> list[str]:
    out = []
    if main is not None:
        from betdata.db.schema import api_quota, api_usage, runner_runs

        rr = main.execute(select(runner_runs.c.status, func.count()).where(
            runner_runs.c.started_at >= since).group_by(runner_runs.c.status)).all()
        tot = sum(n for _, n in rr)
        bad = sum(n for s, n in rr if s != "ok")
        out.append(f"- Hourly runs: {tot}, not ok: {bad}")
        q = main.execute(select(api_quota).where(api_quota.c.source == "the_odds_api")
                         .order_by(api_quota.c.period_start.desc()).limit(1)).first()
        if q is not None:
            out.append(f"- The Odds API this month: used {q.used_reported}, remaining "
                       f"{q.remaining_reported} (limit {q.limit_units})")
        sh = main.execute(select(func.count()).select_from(api_usage).where(
            api_usage.c.source == "sharpapi", api_usage.c.called_at >= since)).scalar()
        out.append(f"- SharpAPI calls by the hourly job this week: {sh}")
    if poller is not None:
        from betdata.alerts.db import poller_runs

        rows = poller.execute(select(poller_runs).where(poller_runs.c.started_at >= since)).all()
        failed = sum(r.status == "failed" for r in rows)
        errs = sum(1 for r in rows if r.message and "failed" in (r.message or ""))
        req: dict = {}
        for r in rows:
            for k, v in (r.requests or {}).items():
                req[k] = req.get(k, 0) + int(v)
        out.append(f"- Poller jobs: {len(rows)}, failed: {failed}, with task errors: {errs}")
        out.append("- Poller requests this week: " + (", ".join(
            f"{k} {v}" for k, v in sorted(req.items())) or "none"))
    return out or ["- (no run data available)"]


def _rec(r) -> str:
    """A graded-only record: W-L-P with units, ROI, average odds, CLV, graded picks (M19)."""
    roi = "–" if r.roi is None else f"{100 * r.roi:+.1f}%"
    odds = "–" if r.avg_odds is None else (f"+{r.avg_odds}" if r.avg_odds > 0 else str(r.avg_odds))
    clv = "–" if r.clv is None else f"{r.clv:+.1f} pp"
    return f"{r.wlp} | {r.units:+.2f}u | {roi} | {odds} | {clv} | {r.graded}"


def _tags(tc: dict) -> str:
    parts = []
    for res, word in (("loss", "losses"), ("win", "wins")):
        d = tc[res]
        if d:
            parts.append(f"{word} " + ", ".join(f"{v} {k}" for k, v in
                                                sorted(d.items(), key=lambda kv: -kv[1])))
    return "; ".join(parts) or "no reason tags yet"


def build(root: Path, now: datetime, main: Connection | None,
          poller: Connection | None) -> tuple[str, list[str]]:  # fmt: skip
    """(report markdown, 6-line summary) on the official card record and the reason tags, with
    one line per research signal (M19, MODEL.md "M19: Weekly report")."""
    from betdata import official as O
    from betdata.alerts.gates import GATE_N, gate

    rows = O.dataset(root / "ledger")
    n_legacy = sum(r["signal_name"] == LEGACY for r in joined(root / "ledger"))
    t = now.astimezone(EASTERN)
    today = t.date()
    from betdata import cards as C

    week_from = (today - timedelta(days=6)).isoformat()

    def wk(rs):
        return [r for r in rs if week_from <= r["day"] <= today.isoformat()]

    # each official card on its own (owner decision 2026-11-05: NBA Next Gen and OG, NFL; no
    # combined "all sports" line)
    cards = [(f"NBA {C.name(k)}", C.view(rows, k)) for k in (C.NEXTGEN, C.OG)]
    cards += [(sp.upper(), O.official(rows, sp)) for sp in sorted(
        {r["sport"] for r in O.official(rows)} - {"nba"})]  # fmt: skip
    off = [r for _, v in cards for r in v]
    week = wk(off)
    head = "| | W-L-P | Units | ROI | Avg odds | CLV | Graded |\n|---|---|---|---|---|---|---|"
    md = [f"# Weekly report, {today.isoformat()}", "",
          f"Written {now.isoformat(timespec='minutes')} by the hourly run. Official picks are "
          "the daily cards (NBA: Next Gen and OG, each on its own). Records count graded picks "
          "only, like a team's record. Paper units, not advice.", "",
          *([f"Excludes {n_legacy} M16 legacy alerts (pre-M18 rules), available in the CSV.",
             ""] if n_legacy else []),
          "## Official records (each card on its own)", "", head]
    for name, v in cards:
        md.append(f"| {name}, last 7 days | {_rec(O.record(wk(v), O.official_stake))} |")
        md.append(f"| {name}, all time | {_rec(O.record(v, O.official_stake))} |")
    # each card's own success goal (owner decisions 2026-11-03 and 2026-11-05)
    md += ["", "## Official success goals (each card)", ""]
    goals = {}
    for name, v in cards:
        g = goals[name] = O.goal(v)
        md.append(f"- {name}: {O.goal_text(g)} ({O.context_text(g)})")
    md += ["", "## Why picks won or lost (official)", "",
           f"- Last 7 days: {_tags(O.tag_counts(week))}",
           f"- All time: {_tags(O.tag_counts(off))}", "",
           "## Results by day (last 7 days)", "", head]
    for d, rs in O.by_day(week).items():  # every card that day, together
        md.append(f"| {d} | {_rec(O.record(rs, O.official_stake))} |")
    md += ["", "## Research signals (not official picks; every pick, flat 1 unit)", "",
           "| Signal | W-L-P | Units | ROI | Avg odds | CLV | Graded | Gate |",
           "|---|---|---|---|---|---|---|---|"]
    sig = O.signals(rows)
    for name, r in sig.items():
        g = gate(r.clvs)
        md.append(f"| {name} | {_rec(r)} | {g['n']}/{GATE_N}: {g['status']} |")
    # Next Gen vs OG, same nights only (owner requests 2026-11-05)
    cmp_ = O.comparison(rows, root / "ledger")
    md += ["", f"## Next Gen vs OG (same nights only: {cmp_.nights})", "",
           "| | Next Gen | OG |", "|---|---|---|",
           *[f"| {m} | {a} | {b} |" for m, a, b in O.comparison_rows(cmp_)], "",
           O.COMPARE_RULE, "", f"Status: {cmp_.verdict}"]
    # rules v2: what each filter removed against the card (owner 2026-11-09)
    from betdata import eligibility as EL

    md += ["", f"## Filters (rules v2, since {EL.RULES_V2_START.isoformat()})", "",
           "| Card | Filter | Removed | Graded W-L-P | Units (flat) | Win vs needed | "
           "Card same period | Review |", "|---|---|---|---|---|---|---|---|"]
    for r in EL.filters_table(rows):
        md.append(f"| {r['card']} | {r['reason']} | {r['removed']} | {r['wlp']} | "
                  f"{r['units']:+.2f} | {EL.pct(r['win'])} vs {EL.pct(r['needed'])} | "
                  f"{r['card_wlp']}, {r['card_units']:+.2f}u | {r['status']} |")
    md += ["", EL.REVIEW_RULE]
    # news alerts: their own record, the switch and the per-source table (owner 2026-11-05)
    from betdata.news import ledger as NL

    na = O.record([r for r in rows if r["signal_name"] == "news-alert"])
    md += ["", "## News alerts (not card picks)", "", NL.switch_text(root / "ledger"), "",
           f"- Record: {_rec(na).replace(' | ', ', ')}", "",
           "| Source | First | Median lead (min) | Confirmed | Contradicted | Unconfirmed | "
           "Official first |", "|---|---|---|---|---|---|---|"]
    for r in NL.source_table(NL.read(root / "ledger")):
        md.append(f"| {r['account'] or r['source']} | {r['first']} | "
                  f"{'–' if r['median_lead'] is None else r['median_lead']} | {r['confirmed']} | "
                  f"{r['contradicted']} | {r['unconfirmed']} | {r['official_first']} |")
    # every NBA engine prediction graded at the close (owner override 2026-11-04)
    from betdata.nba import engine_grades as G

    eg = G.read(root / "ledger")
    md += ["", "## NBA engine: every prediction vs the close", ""]
    for label, kind in (("All", None), ("Game markets", "game"), ("Player props", "prop")):
        md.append(f"- {label}: {G.summary_line(G.summary(eg, kind))}")
    md.append(f"- Parlay bars check (reporting only): {G.band_line(G.band_table(eg))}")
    from betdata import parlay as PL

    md.append(f"- Next Gen parlay (its own record): {PL.record(root / 'ledger').text()}")
    week_eg = [r for r in eg if r["tip"] >= (now - timedelta(days=7)).isoformat()]
    md.append(f"- Last 7 days: {G.summary_line(G.summary(week_eg))}")
    since = now - timedelta(days=7)
    md += ["", "## System health (last 7 days)", "", *health(main, poller, since), ""]

    def short(r) -> str:
        roi = "–" if r.roi is None else f"{100 * r.roi:+.0f}%"
        return f"{r.wlp} {r.units:+.2f}u ROI {roi}"

    summary = [
        f"Weekly {today.isoformat()}: "
        + "; ".join(f"{n} {short(O.record(wk(v), O.official_stake))}" for n, v in cards),
        "All time: " + "; ".join(f"{n} {short(O.record(v, O.official_stake))}"
                                 for n, v in cards),
        ("Goals: " + "; ".join(f"{n} {g.graded}/{O.GOAL_N} {g.status}"
                               for n, g in goals.items()))[:200],
        f"Why (7 days): {_tags(O.tag_counts(week))}"[:180],
        "Research: " + ", ".join(f"{k} {short(r)}" for k, r in sig.items())[:220],
        "Gates: " + ", ".join(f"{k} {gate(r.clvs)['n']}/{GATE_N}" for k, r in sig.items()),
    ]
    return "\n".join(md) + "\n", summary


def ntfy(topic: str | None, title: str, lines: list[str], client=None) -> str:
    """Push to ntfy (the topic comes from the NTFY_TOPIC secret and is never logged)."""
    if not topic:
        return "no NTFY_TOPIC"
    try:
        r = (client or httpx.Client(timeout=15)).post(
            NTFY + topic, content="\n".join(lines).encode(),
            headers={"Title": title, "Click": PAGE + "#value", "Tags": "bar_chart"})
        return f"http {r.status_code}"
    except httpx.HTTPError as e:
        return f"error {type(e).__name__}"


def milestone_lines(ms: list[dict], root: Path) -> list[str]:
    rows = counted(joined(root / "ledger"))
    out = []
    for m in ms:
        from betdata import spread_review as SR

        if m["signal_name"] == SR.MILESTONE:  # the Spread Picks review (owner 2026-11-10)
            out.append(SR.ntfy_line(root / "ledger"))
            continue
        rs = [r for r in rows if r["signal_name"] == m["signal_name"]]
        from betdata.ledger import record

        rec = record(rs)
        n, mu, lo, hi = rec.clv_stats()
        out.append(f"{m['signal_name']} reached {m['graded']} graded picks: "
                   f"{rec.wins}-{rec.losses}-{rec.pushes}, CLV {_pp(mu)} ({_rng(lo, hi)})")
    return out

