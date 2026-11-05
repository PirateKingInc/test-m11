"""The news log in the ledger (owner decision 2026-11-05): every settled early-source claim,
once, in `ledger/news_sources.csv` (append-only), and each news alert's confirmation as an
annotation (`news_status`: confirmed / contradicted / unconfirmed). `source_table` is the
per-source table on the Research tab and in the weekly report."""

from __future__ import annotations

import statistics
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

FILE = "news_sources.csv"
FIELDS = ("key", "source", "account", "group", "player", "team", "game_id", "status",
          "posted_at", "detected_at", "official_before", "ahead", "corroborated", "outcome",
          "official_status", "official_at", "lead_min", "alert_ids", "resolved_at")  # fmt: skip


def _iso(t) -> str:
    from betdata.alerts.grade import utc

    return "" if t is None else utc(t).isoformat()


def sync(p, out: Path, now: datetime) -> int:
    from betdata.alerts.db import early_claims as ec
    from betdata.ledger import ANNOTATION_FIELDS, _tables, append_rows, pick_id

    if "early_claims" not in _tables(p):
        return 0
    rows, notes = [], []
    for c in p.execute(select(ec).where(ec.c.outcome != "pending")
                       .order_by(ec.c.claim_id)).all():  # fmt: skip
        key = f"{c.source}:{c.account}:{c.player_name}:{c.status or 'hint'}:{_iso(c.posted_at)}"
        rows.append({"key": key, "source": c.source, "account": c.account, "group": c.group_,
                     "player": c.player_name, "team": c.team or "",
                     "game_id": c.game_id or "", "status": c.status or "",
                     "posted_at": _iso(c.posted_at), "detected_at": _iso(c.detected_at),
                     "official_before": c.official_before or "",
                     "ahead": str(bool(c.ahead)).lower(),
                     "corroborated": str(bool(c.corroborated)).lower(),
                     "outcome": c.outcome, "official_status": c.official_status or "",
                     "official_at": _iso(c.official_at),
                     "lead_min": "" if c.lead_min is None else c.lead_min,
                     "alert_ids": " ".join(str(a) for a in (c.alert_ids or [])),
                     "resolved_at": _iso(c.resolved_at)})  # fmt: skip
        if c.alert_ids:
            from betdata.alerts.db import alerts

            for a in p.execute(select(alerts.c.dedup_key).where(
                    alerts.c.alert_id.in_(c.alert_ids))).scalars():  # fmt: skip
                pid = pick_id(a)
                notes.append({"key": f"{pid}:news_status", "pick_id": pid,
                              "field": "news_status", "value": c.outcome,
                              "noted_at": now.isoformat(),
                              "note": f"official report vs @{c.account} (news alert)"})
    append_rows(out / "annotations.csv", ANNOTATION_FIELDS, notes, key="key")
    switch_row(p, out)
    return append_rows(out / FILE, FIELDS, rows, key="key")


SWITCH_FILE = "news_switch.csv"
SWITCH_FIELDS = ("key", "decided_at", "soft_events", "soft_median_min", "alerts", "rule")


def switch_row(p, out: Path) -> int:
    """The M16 lag study's decision, written once (it switches news alert pushes)."""
    from betdata.alerts.db import get_state
    from betdata.ledger import append_rows
    from betdata.news.desk import alerts_on

    d = get_state(p, "signal1:decision")
    if not d:
        return 0
    return append_rows(out / SWITCH_FILE, SWITCH_FIELDS, [{
        "key": "lag-decision", "decided_at": d.get("decided_at", ""),
        "soft_events": d.get("soft_events", ""),
        "soft_median_min": "" if d.get("soft_median_min") is None else d["soft_median_min"],
        "alerts": "on" if alerts_on(p) else "off",
        "rule": "pooled DK+FD median lag >= 5 min: alerts on; else off (logging only)"}],
        key="key")  # fmt: skip


def switch_text(out: Path) -> str:
    from betdata.ledger import read_rows

    rows = read_rows(out / SWITCH_FILE)
    if not rows:
        return ("News alert pushes: OFF until the M16 lag study's decision (due Nov 6); "
                "every claim is logged meanwhile.")
    d = rows[-1]
    med = d["soft_median_min"] or "–"
    return (f"News alert pushes: {d['alerts'].upper()} — lag study decided "
            f"{d['decided_at'][:10]}: pooled DK+FD median lag {med} min over "
            f"{d['soft_events']} lags (rule: under 5 min keeps alerts off; logging always on).")


def source_table(rows: list[dict]) -> list[dict]:
    """Per source and account: claims, times it was first (ahead of the official report), the
    median lead in minutes when confirmed, confirmed / contradicted / unconfirmed counts."""
    by: dict[tuple, list[dict]] = {}
    for r in rows:
        by.setdefault((r["source"], r["account"]), []).append(r)
    out = []
    for (src, acct), rs in sorted(by.items()):
        first = [r for r in rs if r["ahead"] == "true"]
        conf = [r for r in first if r["outcome"] == "confirmed"]
        leads = [float(r["lead_min"]) for r in conf if r["lead_min"] not in ("", None)]
        out.append({"source": src, "account": acct, "claims": len(rs), "first": len(first),
                    "official_first": sum(r["outcome"] == "official first" for r in rs),
                    "confirmed": len(conf),
                    "contradicted": sum(r["outcome"] == "contradicted" for r in first),
                    "unconfirmed": sum(r["outcome"] in ("unconfirmed", "no change")
                                       for r in first),
                    "median_lead": round(statistics.median(leads), 1) if leads else None})
    return out


def read(out: Path) -> list[dict]:
    from betdata.ledger import read_rows

    return read_rows(out / FILE)
