"""Alerts (MODEL.md "M16 / Alerts"): store every alert once as a 1-unit paper bet, and push it
to ntfy.sh unless it is quiet hours (23:00-07:00 ET).

De-duplication is the database's: `alerts.dedup_key` is unique per (signal, game, market, side,
triggering change), so a second insert of the same alert is a no-op. The ntfy topic comes from
the `NTFY_TOPIC` secret and is never logged, stored or shown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from betdata.alerts.db import alerts
from betdata.alerts.rules import EASTERN, quiet

log = logging.getLogger(__name__)
PAGE = "https://piratekinginc.github.io/test-m11/"
NTFY = "https://ntfy.sh/"
BOOKS = {"draftkings": "DraftKings", "fanduel": "FanDuel"}
MARKETS = {"spreads": "Spread", "totals": "Total", "h2h": "Moneyline", "points": "Points",
           "rebounds": "Rebounds", "assists": "Assists", "threes": "3-pointers",
           "rec_yds": "Receiving yards", "rush_yds": "Rushing yards",
           "receptions": "Receptions"}  # fmt: skip


@dataclass
class Alert:
    signal: str  # news, sharp
    sport: str
    game_id: int
    game_label: str  # "CLE @ PHI"
    tip: datetime
    market: str
    side: str
    line: float | None
    price: int
    book: str
    reason: str
    dedup_key: str
    fair_prob: float | None = None
    book_prob: float | None = None
    player_name: str | None = None
    event_id: int | None = None
    study: bool = False
    details: dict | None = None


def page_url(now: datetime) -> str:
    return f"{PAGE}?at={now.strftime('%Y%m%dT%H%MZ')}#alerts"


def _fmt_price(p: int) -> str:
    return f"+{p}" if p > 0 else str(p)


def selection(a: Alert) -> str:
    """'CLE +8.5', 'Over 221.5', 'Embiid over 27.5 points', 'PHI ML'."""
    away, home = a.game_label.split(" @ ")
    team = home if a.side == "home" else away
    if a.player_name:
        return f"{a.player_name} {a.side} {a.line:g} {MARKETS.get(a.market, a.market).lower()}"
    if a.market == "totals":
        return f"{a.side.title()} {a.line:g}"
    if a.market == "spreads":
        return f"{team} {a.line:+g}"
    return f"{team} ML"


def message(a: Alert, now: datetime) -> tuple[str, str]:
    """(title, body) of the push."""
    tip = a.tip.astimezone(EASTERN).strftime("%a %-I:%M %p ET")
    title = (f"{a.sport.upper()} {a.game_label}: {selection(a)} {_fmt_price(a.price)} "
             f"at {BOOKS.get(a.book, a.book)}")  # fmt: skip
    kind = "News" if a.signal == "news" else "Soft vs sharp"
    body = (f"{kind} - {MARKETS.get(a.market, a.market)}, tip {tip}.\n{a.reason}\n"
            f"Paper alert, not advice. {page_url(now)}")  # fmt: skip
    return title, body


def record(pconn, a: Alert, now: datetime) -> int | None:
    """Store the alert (paper bet); None if this alert was already stored (de-duplicated)."""
    stmt = sqlite_insert(alerts).values(
        signal=a.signal, sport=a.sport, game_id=a.game_id, game_label=a.game_label, tip=a.tip,
        market=a.market, player_name=a.player_name, side=a.side, line=a.line, price=a.price,
        book=a.book, fair_prob=a.fair_prob, book_prob=a.book_prob, reason=a.reason,
        dedup_key=a.dedup_key[:255], event_id=a.event_id, study=a.study, created_at=now,
        quiet=quiet(now), sent=False, page_url=page_url(now), details=a.details,
    ).on_conflict_do_nothing(index_elements=["dedup_key"])  # fmt: skip
    res = pconn.execute(stmt)
    return res.inserted_primary_key[0] if res.rowcount else None


def push(pconn, alert_id: int, a: Alert, now: datetime, topic: str | None,
         client: httpx.Client | None = None) -> str:  # fmt: skip
    """Push a stored alert unless quiet hours or no topic; record the outcome."""
    if quiet(now):
        status = "quiet hours"
    elif not topic:
        status = "no NTFY_TOPIC"
    else:
        title, body = message(a, now)
        try:
            r = (client or httpx.Client(timeout=15)).post(
                NTFY + topic, content=body.encode(),
                headers={"Title": title, "Click": page_url(now), "Tags": "chart_with_upwards_trend",
                         "Priority": "high" if a.signal == "news" else "default"})  # fmt: skip
            status = f"http {r.status_code}"
        except httpx.HTTPError as e:
            status = f"error {type(e).__name__}"
        sent = status == "http 200"
        pconn.execute(update(alerts).where(alerts.c.alert_id == alert_id).values(
            sent=sent, sent_at=now if sent else None, send_status=status))  # fmt: skip
        log.info("alert %s: %s", alert_id, status)
        return status
    pconn.execute(update(alerts).where(alerts.c.alert_id == alert_id).values(
        send_status=status))  # fmt: skip
    return status


def emit(pengine, a: Alert, now: datetime, topic: str | None,
         client: httpx.Client | None = None) -> int | None:  # fmt: skip
    """Record and push one alert; returns its id, or None if it was a duplicate."""
    with pengine.begin() as pconn:
        alert_id = record(pconn, a, now)
    if alert_id is None:
        return None
    with pengine.begin() as pconn:
        push(pconn, alert_id, a, now, topic, client)
    return alert_id
