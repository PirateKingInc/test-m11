"""Tonight's card (owner request 2026-10-30, MODEL.md "Tonight's card"): an in-house
simulation of a pick service. Display only: no signal, threshold, model or grade changes.

Once a day per sport, at 8:50 AM US Eastern on days that sport has games, the poller selects
the card: the top 5 picks by expected value among that day's M18 picks (C, F, G2, G1) still
"Live" (the alerted price, or better, is the book's latest stored quote) for games not yet
started. If none is Live at 8:50, the card is selected at the first poll that day with any Live
pick. Picks created after selection stay off the card; fewer than 5 qualifying means a shorter
card. Each card pick gets a label and stake at selection:
- **TAKE** (1 unit): edge >= 1.5x its signal's threshold AND (edge source "news" OR a fresh
  Pinnacle price sides with the pick);
- **LEAN** (0.5 unit): every other card pick; book disagreement against a two-book consensus is
  LEAN at most.
(The same tests as alerts/labels.py, at selection time; the price is Live by construction.)

Expected value per unit staked = fair probability x decimal odds - 1. A pick with no fair
probability (G1: its rule is a points drop, not a priced edge) has no expected value and is never
on the card.

Right after selection the card is pushed to ntfy, one message per sport. The selection is stored
once in the poller state (`card:<sport>:<day>`), saved with the poller database at :56, and
written once to the ledger (`ledger/card.csv`, betdata/ledger.py) by the next hourly run, which
publishes it: an 8:50 card is on the 9:00 AM page. A card prop whose player does not play (ruled
out) is graded void, as books do (alerts/grade.py `prop_actual`); a void is kept but stays out of
W-L-P, units and ROI. Cards start on 2026-10-31 (no backfill).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from betdata.alerts.db import alerts, get_state, set_state
from betdata.alerts.labels import LEAN, SIGNALS, STAKE, TAKE, available, label
from betdata.alerts.rules import EASTERN, decimal

SIZE = 5
AT = time(8, 50)  # Eastern
FIRST_DAY = date(2026, 10, 31)
SPORTS = ("nba", "nfl")


def key(sport: str, day: date) -> str:
    return f"card:{sport}:{day.isoformat()}"


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), EASTERN)
    return start, start + timedelta(days=1)


def select_at(day: date) -> datetime:
    """The card's selection time on the Eastern day: 8:50 AM ET."""
    return datetime.combine(day, AT, EASTERN)


def first_tip(main, sport: str, day: date) -> datetime | None:
    """The sport's first scheduled start on the Eastern day (main database)."""
    from betdata.alerts.grade import utc
    from betdata.db.schema import games, sports

    lo, hi = day_bounds(day)
    t = main.execute(select(games.c.start_time_utc)
                     .join(sports, sports.c.sport_id == games.c.sport_id)
                     .where(sports.c.key == sport, games.c.start_time_utc >= lo,
                            games.c.start_time_utc < hi,
                            games.c.status.not_in(("postponed", "cancelled")))
                     .order_by(games.c.start_time_utc)).first()  # fmt: skip
    return None if t is None else utc(t[0])


def ev(a) -> float | None:
    """Expected value per unit staked at the alerted price."""
    if a.fair_prob is None or a.price is None:
        return None
    return a.fair_prob * decimal(a.price) - 1


def _ns(r) -> SimpleNamespace:
    from betdata.alerts.grade import utc

    d = dict(r._mapping)
    d["tip"] = None if d["tip"] is None else utc(d["tip"])
    d["details"] = d.get("details") or {}
    return SimpleNamespace(**d)


def day_picks(p, sport: str, day: date) -> list[SimpleNamespace]:
    """The sport's M18 picks for games on the Eastern day."""
    lo, hi = day_bounds(day)
    return [_ns(r) for r in p.execute(select(alerts).where(
        alerts.c.sport == sport, alerts.c.signal.in_(SIGNALS), alerts.c.tip >= lo,
        alerts.c.tip < hi).order_by(alerts.c.created_at, alerts.c.alert_id))]  # fmt: skip


def choose(p, sport: str, day: date, now: datetime) -> dict:
    """The card at `now` (pure on the poller database): top SIZE live picks by EV."""
    from betdata.alerts.grade import utc

    pool = []
    for a in day_picks(p, sport, day):
        if utc(a.created_at) > now or a.tip is None or a.tip <= now:
            continue
        x = ev(a)
        if x is None or available(p, a, now) is not True:
            continue
        pool.append((x, a))
    pool.sort(key=lambda t: -t[0])
    picks = []
    for x, a in pool[:SIZE]:
        lab = label(p, a, now) or {}
        lb = TAKE if lab.get("label") == TAKE else LEAN
        picks.append({"dedup_key": a.dedup_key, "label": lb, "stake": STAKE[lb],
                      "ev": round(x, 4), "edge": lab.get("edge"),
                      "threshold": lab.get("threshold"), "edge_source": lab.get("edge_source"),
                      "pinnacle_sides": lab.get("pinnacle_sides")})  # fmt: skip
    return {"sport": sport, "day": day.isoformat(), "selected_at": now.isoformat(),
            "picks": picks, "live": len(pool)}


def in_ledger(sport: str, day: date) -> bool:
    """A rebuilt poller database must not choose a day's card again."""
    from betdata.ledger import ledger_dir, read_rows

    return any(r["sport"] == sport and r["card_date"] == day.isoformat()
               for r in read_rows(ledger_dir() / "card.csv"))  # fmt: skip


def run(pdb, main, now: datetime, tip_fn=first_tip) -> list[dict]:
    """Choose each sport's card when due (once): from 8:50 AM ET on a day the sport has games,
    at the first poll with any Live pick. Returns the cards chosen now."""
    day = now.astimezone(EASTERN).date()
    if day < FIRST_DAY:
        return []
    out = []
    for sport in SPORTS:
        with pdb.connect() as p:
            if get_state(p, key(sport, day)) is not None:
                continue
        if in_ledger(sport, day):
            continue
        with main.connect() as m:
            ft = tip_fn(m, sport, day)
        if ft is None or now < select_at(day):  # no games today, or not 8:50 yet
            continue
        with pdb.begin() as p:
            card = choose(p, sport, day, now)
            if not card["picks"]:  # nothing Live yet: try again at the next poll
                continue
            set_state(p, key(sport, day), card, now)
        out.append(card)
    return out


def message(p, card: dict, now: datetime) -> tuple[str, str]:
    """The ntfy push for one sport's card: (title, body), one line per pick."""
    from betdata.alerts.notify import BOOKS, _fmt_price, selection

    by_key = {a.dedup_key: a for a in day_picks(p, card["sport"],
                                                datetime.fromisoformat(card["day"]).date())}
    lines = []
    for x in card["picks"]:
        a = by_key.get(x["dedup_key"])
        if a is None:
            continue
        when = a.tip.astimezone(EASTERN).strftime("%-I:%M %p ET")
        lines.append(f"{a.game_label} {when}: {selection(a)} {_fmt_price(a.price)} at "
                     f"{BOOKS.get(a.book, a.book)}, {x['label']} {x['stake']:g}u")
    day = datetime.fromisoformat(card["day"]).strftime("%a %b %-d")
    return f"Tonight's card - {card['sport'].upper()} - {day}", "\n".join(lines)


def push(pdb, card: dict, now: datetime, topic: str | None, client=None) -> str:
    """Push one sport's card to ntfy and record the outcome on the stored card."""
    import httpx

    from betdata.alerts.notify import NTFY, PAGE

    with pdb.connect() as p:
        title, body = message(p, card, now)
    if not topic:
        status = "no NTFY_TOPIC"
    else:
        try:
            r = (client or httpx.Client(timeout=15)).post(
                NTFY + topic, content=body.encode(),
                headers={"Title": title, "Click": PAGE, "Tags": "clipboard"})
            status = f"http {r.status_code}"
        except Exception as e:  # noqa: BLE001 - a failed push never loses the card
            status = f"error {type(e).__name__}"
    with pdb.begin() as p:
        k = key(card["sport"], datetime.fromisoformat(card["day"]).date())
        st = get_state(p, k) or card
        set_state(p, k, {**st, "push": status}, now)
    return status


def due_between(main, start: datetime, end: datetime, tip_fn=first_tip) -> bool:
    """Is an 8:50 AM ET selection due in [start, end] on a day with games (keeps the poller
    awake for it)?"""
    for day in {start.astimezone(EASTERN).date(), end.astimezone(EASTERN).date()}:
        if day < FIRST_DAY or not start <= select_at(day) <= end:
            continue
        for sport in SPORTS:
            with main.connect() as m:
                if tip_fn(m, sport, day) is not None:
                    return True
    return False
