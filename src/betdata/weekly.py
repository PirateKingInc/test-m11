"""The weekly report (owner request 2026-10-30): on Mondays, after the morning hourly run,
`reports/weekly/YYYY-MM-DD.md` with:
- per-signal and all-time records, with CLV and 95% ranges (vs the consensus close and,
  where one exists, Pinnacle's close);
- the C signals split by edge source (news vs book disagreement);
- system health: failed hourly runs and poller jobs, quota use.

The hourly job commits the report and pushes a 5-line ntfy summary. It also pushes an extra
ntfy alert when a signal reaches 60 or 200 graded picks (ledger milestones).
Reporting only: nothing here changes a rule, a threshold or a model.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import Connection, func, select

from betdata.alerts.rules import EASTERN
from betdata.ledger import TOO_FEW, Record, breakdowns, joined

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


def build(root: Path, now: datetime, main: Connection | None,
          poller: Connection | None) -> tuple[str, list[str]]:  # fmt: skip
    """(report markdown, 5-line summary)."""
    rows = joined(root / "ledger")
    since = now - timedelta(days=7)
    week = [r for r in rows if r["created_at"] and datetime.fromisoformat(r["created_at"])
            >= since]  # fmt: skip
    b_all, b_week = breakdowns(rows), breakdowns(week)
    t = now.astimezone(EASTERN)
    md = [f"# Weekly report, {t.date().isoformat()}",
          "",
          f"Written {now.isoformat(timespec='minutes')} by the hourly run. Reporting only; the "
          "signals are frozen until their 200-pick gates. Paper picks, not advice. Records "
          f"under {TOO_FEW} graded picks are too few to judge.",
          "", "## All-time", "", HEAD]
    for group, recs in b_all.items():
        for k, r in recs.items():
            md.append(record_line(k if group == "all" else f"{group}: {k}", r))
    md += ["", "## Last 7 days", "", HEAD]
    for group in ("all", "by signal"):
        for k, r in b_week.get(group, {}).items():
            md.append(record_line(k, r))
    md += ["", "## C signals: news vs book disagreement (all-time)", "", HEAD]
    for k, r in b_all["C signals by edge source"].items():
        md.append(record_line(k, r))
    md += ["", "## System health (last 7 days)", "", *health(main, poller, since), ""]
    a = b_all["all"]["all picks"]
    n, mu, lo, hi = a.clv_stats()
    news = b_all["C signals by edge source"].get("news")
    book = b_all["C signals by edge source"].get("book disagreement")
    summary = [
        f"Weekly {t.date().isoformat()}: {len(week)} new picks, {a.picks} all-time",
        f"All-time {a.wins}-{a.losses}-{a.pushes}, {a.units:+.2f}u, ROI "
        + ("–" if a.roi is None else f"{100 * a.roi:+.1f}%"),
        f"CLV vs consensus {_pp(mu)} ({_rng(lo, hi)}), n={n}",
        "C news/book: " + " / ".join(f"{r.picks} picks {_pp(r.clv_stats()[1])}" if r else "0"
                                     for r in (news, book)),
        "Signals: " + ", ".join(f"{k} {r.graded}/200" for k, r in
                                b_all["by signal"].items())[:200],
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
    rows = joined(root / "ledger")
    out = []
    for m in ms:
        rs = [r for r in rows if r["signal_name"] == m["signal_name"]]
        from betdata.ledger import record

        rec = record(rs)
        n, mu, lo, hi = rec.clv_stats()
        out.append(f"{m['signal_name']} reached {m['graded']} graded picks: "
                   f"{rec.wins}-{rec.losses}-{rec.pushes}, CLV {_pp(mu)} ({_rng(lo, hi)})")
    return out

