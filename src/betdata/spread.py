"""The Spread Picks card (owner decision 2026-11-10, MODEL.md "Spread Picks card";
config/cards.yaml `spread`): a third official NBA card with its own record.

- **Trigger:** a Next Gen *official* card pick (TAKE or LEAN) on `h2h` at positive American
  odds. It creates exactly one Spread pick on the same team (the same side) to cover. Never from
  OG, non-card picks, the parlay, news alerts or favourites; the ML pick stays on Next Gen.
- **Pricing:** the spread at the ML pick's book and snapshot (the latest stored quote at or
  before the ML pick's `created_at`: quotes are stored change-only, so that row is the line in
  force then), else the other of DraftKings / FanDuel at that snapshot; neither -> no pick and a
  `spread_skipped` annotation on the ML pick. The real line and price are stored.
- **Stake:** 1 unit flat; graded by the normal path (alerts/grade.py): win = decimal - 1,
  loss = -1, push = 0; a voided ML pick voids its Spread pick (`propagate_voids`).
- **When:** right after the Next Gen card, in the 6:15 AM ET morning run (or at the later poll
  that chooses Next Gen): it follows Next Gen's waiting rules and is empty when nothing
  qualifies. Stored once in the poller state `spread:nba:<day>`, written once to
  `ledger/spread_card.csv` (and `card_days.csv`, kind `spread`).
- **History:** the Next Gen cards of Nov 6 to Nov 10 are backfilled (`backfill`) from card.csv
  and the stored quotes, annotated `spread_backfill`; they count.

Spread picks are ordinary alerts (signal `dogsprd`, research name `dog-spread`), so they are
ledger picks (`ledger/picks.csv`) graded into `ledger/results.csv`. Their record is separate:
never in Next Gen, OG, the goals, the parlay or the research tables (ledger.counted).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

log = logging.getLogger(__name__)

SIGNAL = "dogsprd"  # alerts.signal is String(8)
NAME = "dog-spread"  # research / ledger signal name
CARD = "spread"
STAKE = 1.0
LABEL = "SPREAD"
SINCE = date(2026, 11, 6)  # the first Next Gen ML underdog (owner: "since Nov 6")
LIVE_START = date(2026, 11, 11)  # the first live selection; earlier card days are backfilled
BOOKS = ("draftkings", "fanduel")
BOOK_NAMES = {"draftkings": "DraftKings", "fanduel": "FanDuel"}
NO_SPREAD = "no DraftKings or FanDuel spread stored at the ML pick's snapshot"
NO_SPREAD_EVER = "no DraftKings or FanDuel spread stored for the game"
SPREAD_FIELDS = ("pick_id", "sport", "card_date", "spread", "spread_label", "spread_stake",
                 "selected_at", "trigger_pick_id", "book", "snapshot_at", "price_source",
                 "backfill")  # fmt: skip


def key(day: date | str) -> str:
    return f"spread:nba:{day if isinstance(day, str) else day.isoformat()}"


def dedup(trigger_pid: str) -> str:
    """One Spread pick per ML pick: the de-duplication key is the ML pick's id."""
    return f"{SIGNAL}:{trigger_pid}"


def active() -> bool:
    from betdata import cards as C

    c = C.load().get(CARD)
    return c is not None and c.active


def is_trigger(a, pick: dict | None) -> bool:
    """A Next Gen official card pick (TAKE or LEAN), h2h, at positive American odds."""
    from betdata.alerts.labels import ENGINE, LEAN, TAKE

    return (pick is not None and pick.get("label") in (TAKE, LEAN) and a.signal == ENGINE
            and a.sport == "nba" and a.market == "h2h" and a.price is not None
            and int(a.price) > 0 and a.side in ("home", "away"))  # fmt: skip


def _utc(t):
    from betdata.alerts.grade import utc

    return None if t is None else utc(t)


def _quote(p, a, book: str, *, after: bool = False):
    """The spread quote of the ML pick's team at `book`: the latest at or before the ML pick's
    created_at, or (`after`) the earliest after it."""
    from betdata.alerts.db import quotes as q
    from betdata.alerts.grade import QUOTE_SOURCES

    t = _utc(a.created_at)
    cond = [q.c.game_id == a.game_id, q.c.market == "spreads", q.c.outcome == a.side,
            q.c.book == book, q.c.player_name.is_(None), q.c.line.is_not(None),
            q.c.price.is_not(None), q.c.source.in_(QUOTE_SOURCES)]  # fmt: skip
    if after:
        cond.append(q.c.polled_at > t)
        order = (q.c.polled_at, q.c.quote_id)
    else:
        cond.append(q.c.polled_at <= t)
        order = (q.c.polled_at.desc(), q.c.quote_id.desc())
    return p.execute(select(q).where(*cond).order_by(*order).limit(1)).first()


def price(p, a, *, backfill: bool = False) -> tuple[object | None, str]:
    """(quote, source) of the Spread pick, or (None, why). Same book and snapshot, then the
    other of DK/FD; a backfill may also take the nearest later stored quote (same book first),
    i.e. the earliest stored spread after the pick."""
    books = [a.book, *(b for b in BOOKS if b != a.book)] if a.book in BOOKS else list(BOOKS)
    for i, b in enumerate(books):
        r = _quote(p, a, b)
        if r is not None:
            return r, "same book" if i == 0 and b == a.book else "other book"
    if backfill:
        for b in books:
            r = _quote(p, a, b, after=True)
            if r is not None:
                return r, "nearest later quote"
        return None, NO_SPREAD_EVER
    return None, NO_SPREAD


def team_of(a) -> str:
    aw, hm = (a.game_label or "? @ ?").split(" @ ")
    return hm if a.side == "home" else aw


def _insert(p, a, trigger_pid: str, q, src: str, day: str, now: datetime,
            backfill: bool) -> str:  # fmt: skip
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from betdata.alerts.db import alerts
    from betdata.alerts.notify import _fmt_price

    team = team_of(a)
    snap = _utc(q.polled_at).isoformat()
    reason = (f"Spread Picks: {team} to cover, from Next Gen ML underdog {trigger_pid} "
              f"({team} ML {_fmt_price(int(a.price))} at {BOOK_NAMES.get(a.book, a.book)}); "
              f"spread at {BOOK_NAMES.get(q.book, q.book)} {snap} ({src})")
    dk = dedup(trigger_pid)
    # the other side at the same book and snapshot: the book's no-vig probability (CLV and EV
    # at the close are graded from it, as for every pick)
    other = _quote(p, SimpleNamespace(game_id=a.game_id, side="away" if a.side == "home"
                                      else "home", created_at=q.polled_at), q.book)
    book_prob = None
    if other is not None:
        from betdata.alerts.rules import decimal

        x, y = 1 / decimal(int(q.price)), 1 / decimal(int(other.price))
        book_prob = x / (x + y)
    p.execute(sqlite_insert(alerts).values(
        signal=SIGNAL, sport="nba", game_id=a.game_id, game_label=a.game_label, tip=a.tip,
        market="spreads", player_name=None, side=a.side, line=q.line, price=int(q.price),
        book=q.book, fair_prob=None, book_prob=book_prob, reason=reason, dedup_key=dk,
        created_at=now, quiet=True, sent=False, send_status="spread card (morning push)",
        details={"trigger": trigger_pid, "trigger_key": a.dedup_key, "card_date": day,
                 "snapshot_at": snap, "quote_source": q.source, "price_source": src,
                 "backfill": backfill,
                 "other_price": None if other is None else int(other.price)},
    ).on_conflict_do_nothing(index_elements=["dedup_key"]))  # fmt: skip
    return dk


def build(p, day: date, ng_picks: list[dict], now: datetime, *, backfill: bool = False,
          ng: dict | None = None) -> dict:  # fmt: skip
    """The Spread card for `day` from Next Gen's chosen picks (each {dedup_key, label}); creates
    the Spread pick alerts. Pure on the poller database."""
    from betdata.alerts.card import day_picks
    from betdata.ledger import pick_id

    by_key = {a.dedup_key: a for a in day_picks(p, "nba", day, ("engine",))}
    picks, skipped = [], []
    for x in ng_picks:
        a = by_key.get(x["dedup_key"])
        if a is None or not is_trigger(a, x):
            continue
        pid = pick_id(a.dedup_key)
        q, src = price(p, a, backfill=backfill)
        if q is None:
            skipped.append({"trigger": pid, "reason": src})
            continue
        dk = _insert(p, a, pid, q, src, day.isoformat(), now, backfill)
        picks.append({"dedup_key": dk, "trigger": pid, "trigger_key": a.dedup_key,
                      "label": LABEL, "stake": STAKE, "book": q.book, "line": q.line,
                      "price": int(q.price), "snapshot_at": _utc(q.polled_at).isoformat(),
                      "price_source": src})  # fmt: skip
    card = {"sport": "nba", "day": day.isoformat(), "selected_at": now.isoformat(),
            "source": CARD, "picks": picks, "skipped": skipped}
    if backfill:
        card["backfill"] = True
    if ng and ng.get("no_lines"):
        card["no_lines"] = True
    return card


def in_ledger(day: date, out: Path | None = None) -> bool:
    from betdata.ledger import ledger_dir, read_rows

    return any(r["card_date"] == day.isoformat()
               for r in read_rows((out or ledger_dir()) / "card_days.csv")
               if r["kind"] == CARD)  # fmt: skip


def run(pdb, main, now: datetime) -> list[dict]:
    """Choose today's Spread card once, as soon as Next Gen's card is chosen (the morning run
    right after Next Gen, or the later poll that chooses it). Returns the cards chosen now."""
    from betdata.alerts import card as CD
    from betdata.alerts.db import get_state, set_state
    from betdata.alerts.rules import EASTERN

    day = now.astimezone(EASTERN).date()
    if day < LIVE_START or not active():
        return []
    with pdb.begin() as p:
        if get_state(p, key(day)) is not None or in_ledger(day):
            return []
        ng = get_state(p, CD.key("nba", day))
        if ng is None:  # Next Gen not chosen yet: the Spread card waits with it
            return []
        card = build(p, day, ng.get("picks", []), now, ng=ng)
        set_state(p, key(day), card, now)
    return [card]


def backfill(pdb, out: Path, now: datetime) -> list[dict]:
    """The Next Gen card days from SINCE to LIVE_START (exclusive), from ledger/card.csv: their
    ML underdogs' Spread picks, priced from the stored quotes (module docstring). Idempotent:
    a day already stored (poller state or ledger) is skipped."""
    from betdata.alerts.db import get_state, set_state
    from betdata.ledger import read_rows

    days: dict[str, list[dict]] = {}
    for r in read_rows(out / "card.csv"):
        if r["sport"] == "nba" and SINCE.isoformat() <= r["card_date"] < LIVE_START.isoformat():
            days.setdefault(r["card_date"], [])
            if r["card"] == "yes":
                days[r["card_date"]].append(r)
    if not days or not active():
        return []
    picks = {r["pick_id"]: r for r in read_rows(out / "picks.csv")}
    done = []
    with pdb.begin() as p:
        for d, rs in sorted(days.items()):
            day = date.fromisoformat(d)
            if get_state(p, key(day)) is not None or in_ledger(day, out):
                continue
            ng = [{"dedup_key": picks[r["pick_id"]]["dedup_key"], "label": r["card_label"]}
                  for r in rs if r["pick_id"] in picks]  # fmt: skip
            card = build(p, day, ng, now, backfill=True)
            set_state(p, key(day), card, now)
            done.append(card)
    return done


def propagate_voids(pdb) -> int:
    """A voided triggering ML pick voids its Spread pick (written after the normal grading)."""
    from sqlalchemy import update
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from betdata.alerts.db import alert_grades, alerts
    from betdata.ledger import pick_id

    n = 0
    with pdb.begin() as p:
        grades = {r.alert_id: r for r in p.execute(select(alert_grades))}
        sp = p.execute(select(alerts).where(alerts.c.signal == SIGNAL)).all()
        if not sp:
            return 0
        eng = {pick_id(a.dedup_key): a for a in p.execute(select(alerts).where(
            alerts.c.signal == "engine", alerts.c.game_id.in_({s.game_id for s in sp})))}
        for s in sp:
            t = eng.get(s.dedup_key.split(":", 1)[1])
            g = None if t is None else grades.get(t.alert_id)
            if g is None or g.result != "void":
                continue
            old = grades.get(s.alert_id)
            if old is not None and old.result == "void":
                continue
            vals = {"result": "void", "profit": 0.0}
            if old is None:
                p.execute(sqlite_insert(alert_grades).values(alert_id=s.alert_id,
                                                             graded_at=g.graded_at, **vals))
            else:
                p.execute(update(alert_grades).where(alert_grades.c.alert_id == s.alert_id)
                          .values(**vals))  # fmt: skip
            n += 1
    return n


# --- the ledger: spread_card.csv, card days, annotations ----------------------------------------


def ledger_rows(p, out: Path, now: datetime) -> int:
    """Write-once rows: one per Spread pick (spread_card.csv), one card-day row per chosen day,
    a `spread_skipped` annotation on each ML pick without a spread, and a `spread_backfill`
    annotation on each reconstructed pick."""
    from betdata.alerts.db import state
    from betdata.ledger import (
        ANNOTATION_FIELDS,
        CARD_DAY_FIELDS,
        _iso,
        _num,
        append_rows,
        pick_id,
    )

    rows, days, notes = [], [], []
    for st in p.execute(select(state).where(state.c.key.like("spread:%"))):
        c = st.value or {}
        if not c.get("day"):
            continue
        bf = bool(c.get("backfill"))
        for x in c.get("picks", []):
            pid = pick_id(x["dedup_key"])
            rows.append({"pick_id": pid, "sport": "nba", "card_date": c["day"], "spread": "yes",
                         "spread_label": x.get("label", LABEL),
                         "spread_stake": _num(x.get("stake", STAKE)),
                         "selected_at": c.get("selected_at", ""),
                         "trigger_pick_id": x["trigger"], "book": x.get("book", ""),
                         "snapshot_at": x.get("snapshot_at", ""),
                         "price_source": x.get("price_source", ""),
                         "backfill": "true" if bf else "false"})  # fmt: skip
            if bf:
                notes.append({"key": f"{pid}:spread_backfill", "pick_id": pid,
                              "field": "spread_backfill",
                              "value": f"reconstructed price: {x.get('book', '')} "
                                       f"{x.get('snapshot_at', '')} ({x.get('price_source', '')})",
                              "noted_at": _iso(now),
                              "note": "Spread Picks history (owner 2026-11-10): priced from the "
                                      "stored quotes after the fact; counts"})  # fmt: skip
        for s in c.get("skipped", []):
            notes.append({"key": f"{s['trigger']}:spread_skipped", "pick_id": s["trigger"],
                          "field": "spread_skipped", "value": s["reason"],
                          "noted_at": _iso(now),
                          "note": "Spread Picks (owner 2026-11-10): no Spread pick created"})
        days.append({"key": f"nba:{c['day']}:{CARD}", "sport": "nba", "card_date": c["day"],
                     "kind": CARD, "selected_at": c.get("selected_at", ""),
                     "picks": str(len(c.get("picks", [])))})  # fmt: skip
    n = append_rows(out / "spread_card.csv", SPREAD_FIELDS, rows)
    append_rows(out / "card_days.csv", CARD_DAY_FIELDS, days, key="key")
    append_rows(out / "annotations.csv", ANNOTATION_FIELDS, notes, key="key")
    return n


def rows_of(out: Path) -> dict[str, dict]:
    from betdata.ledger import read_rows

    return {r["pick_id"]: r for r in read_rows(out / "spread_card.csv")}


# --- display ---------------------------------------------------------------------------------


def pick_text(team: str, line: float | None, price: int, book: str) -> str:
    """'CHA +8.5 -110 at DraftKings'."""
    from betdata.alerts.notify import _fmt_price

    ln = "" if line is None else (" PK" if line == 0 else f" {line:+g}")
    return f"{team}{ln} {_fmt_price(int(price))} at {BOOK_NAMES.get(book, book)}"


def day_alerts(p, day: date) -> dict[str, SimpleNamespace]:
    from betdata.alerts.card import day_picks

    return {a.dedup_key: a for a in day_picks(p, "nba", day, (SIGNAL, "engine"))}


REVIEW_N = 50  # graded Spread picks before the review (owner 2026-11-10)


def _wl(rec) -> str:
    return f"{rec.wins}-{rec.losses}" + (f"-{rec.pushes}" if rec.pushes else "")


def _u(x: float) -> str:
    return f"{x:+.1f}u" if abs(x) >= 0.05 or x == 0 else f"{x:+.2f}u"


def push_lines(p, rows: list[dict] | None, today: date) -> list[str]:
    """The "Spread picks" section of the NBA push, after Next Gen and OG (owner 2026-11-10):

    Spread Picks: last night 1-1, -0.1u · record 4-2, +1.7u (6 of 50)
    Spread picks: 1 —
      CLE @ CHA 7:00 PM ET: CHA +8.5 (-110) at DraftKings · 1u (Next Gen ML CHA +284)
    or "Spread picks: none today". `rows`: the ledger dataset (None: no record line)."""
    from datetime import timedelta

    from betdata.alerts.db import get_state
    from betdata.alerts.notify import _fmt_price
    from betdata.alerts.rules import EASTERN

    if not active():
        return []
    out = []
    if rows is not None:
        from betdata import cards as C
        from betdata import official as O

        view = C.view(rows, C.SPREAD)
        yday = (today - timedelta(days=1)).isoformat()
        last = O.record([r for r in view if r["day"] == yday], O.official_stake)
        rec = O.record(view, O.official_stake)
        out.append(f"{C.name(C.SPREAD)}: last night {_wl(last)}, {_u(last.units)} · record "
                   f"{_wl(rec)}, {_u(rec.units)} ({rec.graded} of {REVIEW_N})")
    card = get_state(p, key(today))
    if card is None:
        out.append("Spread picks: chosen with the Next Gen card")
        return out
    by = day_alerts(p, today)
    lines = []
    for x in card.get("picks", []):
        a, t = by.get(x["dedup_key"]), by.get(x.get("trigger_key", ""))
        if a is None:
            continue
        team = team_of(a)
        when = a.tip.astimezone(EASTERN).strftime("%-I:%M %p ET") if a.tip else ""
        ln = " PK" if a.line == 0 else f" {a.line:+g}"
        ml = "" if t is None else f" (Next Gen ML {team} {_fmt_price(int(t.price))})"
        lines.append(f"  {a.game_label} {when}: {team}{ln} ({_fmt_price(int(a.price))}) at "
                     f"{BOOK_NAMES.get(a.book, a.book)} · {x.get('stake', STAKE):g}u{ml}")
    if not lines:
        out.append("Spread picks: none today")
    else:
        out.append(f"Spread picks: {len(lines)} —")
        out += lines
    return out
