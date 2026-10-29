"""Our own line archive (M18 Part E, MODEL.md "M18 Part E: our own line archive").

- **Openers:** every quote the poller stores also writes the first price of its (source, book,
  game, market, player, side) to `openers`, once; later prices never overwrite it.
- **Sweeps:** at 06:00 ET, 23:30 ET and once an hour 07:00-23:00 ET, every NBA and NFL game
  with a tip in the next 36 hours is polled for DK/FD main lines and player props (SharpAPI),
  a few games a poller cycle so the 3-minute NBA report cadence keeps its slot. Sweep quotes are
  stored change-only (a row when a key's line or price differs from its last stored row), so
  the archive grows with the market, not with the poll count.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from betdata.alerts.db import openers, quotes
from betdata.alerts.feeds import Quote
from betdata.alerts.rules import EASTERN

SWEEP_HORIZON = timedelta(hours=36)
SWEEP_GAMES_PER_CYCLE = 3
SWEEP_SPORTS = ("nba", "nfl")


def _pkey(name: str | None) -> str:
    from betdata.alerts.poller import _norm

    return _norm(name) if name else ""


def record_openers(p: Connection, qs: list[Quote], now: datetime) -> int:
    """Insert the first price of each key not yet in `openers`; returns rows added."""
    rows, seen = [], set()
    for q in qs:
        k = (q.source, q.book, q.game_id, q.market, _pkey(q.player_name), q.outcome)
        if k in seen:
            continue
        seen.add(k)
        rows.append(dict(sport=q.sport, game_id=q.game_id, source=q.source, book=q.book,
                         market=q.market, player_key=k[4], player_name=q.player_name,
                         outcome=q.outcome, line=q.line, price=q.price, first_seen=now))
    if not rows:
        return 0
    before = p.execute(select(func.count()).select_from(openers)).scalar_one()
    for i in range(0, len(rows), 500):
        p.execute(sqlite_insert(openers).values(rows[i:i + 500]).on_conflict_do_nothing())
    return p.execute(select(func.count()).select_from(openers)).scalar_one() - before


def changed_only(p: Connection, qs: list[Quote]) -> list[Quote]:
    """The quotes whose (line, price) differ from their key's last stored row in `quotes`."""
    gids = sorted({q.game_id for q in qs})
    if not gids:
        return []
    last_id = (select(func.max(quotes.c.quote_id).label("qid"))
               .where(quotes.c.game_id.in_(gids))
               .group_by(quotes.c.source, quotes.c.book, quotes.c.game_id, quotes.c.market,
                         quotes.c.player_name, quotes.c.outcome)).subquery()  # fmt: skip
    last = {}
    for r in p.execute(select(quotes).join(last_id, last_id.c.qid == quotes.c.quote_id)):
        last[(r.source, r.book, r.game_id, r.market, _pkey(r.player_name), r.outcome)] = (
            r.line, r.price)
    out = []
    for q in qs:
        k = (q.source, q.book, q.game_id, q.market, _pkey(q.player_name), q.outcome)
        if last.get(k) != (q.line, q.price):
            last[k] = (q.line, q.price)
            out.append(q)
    return out


def sweep_slot(now: datetime) -> str | None:
    """The sweep slot `now` falls in ("2026-11-01:0600", ...: 06:00, each hour 07-23, 23:30
    ET), or None between 00:00 and 06:00 ET."""
    t = now.astimezone(EASTERN)
    if t.hour < 6:
        return None
    if t.hour == 23 and t.minute >= 30:
        slot = "2330"
    else:
        slot = f"{t.hour:02d}00"
    return f"{t.date().isoformat()}:{slot}"


def sweep_pending(state_slot: str | None, queue: list, start: datetime, end: datetime) -> bool:
    """Is sweep work due between start and end: a queued game, or a slot not yet started?"""
    if queue:
        return True
    t = start
    while t <= end:
        s = sweep_slot(t)
        if s is not None and s != state_slot:
            return True
        t += timedelta(minutes=5)
    s = sweep_slot(end)
    return s is not None and s != state_slot
