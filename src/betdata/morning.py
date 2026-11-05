"""The combined morning update (owner decision 2026-11-05, MODEL.md "Morning update"): one run at
6:15 AM ET on every day with NBA or NFL games yesterday or today, replacing the 8:50 AM card
selection. No change to models, pick rules, thresholds or grading rules.

In this order (`run`):
  a. **grade**: refresh scores and box scores (the existing sources: MORNING_JOBS), then grade
     every pick (the poller's grading, alerts/grade.py). A game without a final box score stays
     ungraded and is graded on the next run (the push says so);
  b. **ledger**: append the new picks and results to the ledger (the official records are built
     from it);
  c. **engine**: re-run the NBA engine on today's games and price the lines already in the
     archive (the poller's 6:00-6:10 AM ET line sweep, alerts/archive.py);
  d. **select**: today's engine card and the shadow Card v1, at the same moment (6:15 replaces
     8:50; top 5 by EV still Live, fewer if fewer qualify, even none, once lines exist for any
     of today's games; with no lines at all, the first later poll with lines selects, and 60
     minutes before the first tip an empty "No lines posted in time" card; alerts/card.py
     `decide`);
  e. **publish**: the ledger again (card rows), then the page (the workflow deploys it);
  f. **push**: ONE ntfy message per sport combining last night, the record and today's card
     (`push`, run by the workflow after the deploy).

The clock dispatches it (`hourly.yml`, trigger `morning`); the poller's 6 AM job stops at
6:12 AM ET on those days so the morning run owns the poller database (alerts/cli.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select

log = logging.getLogger(__name__)
AT = time(6, 15)  # Eastern: the morning run
POLLER_STOP = time(6, 12)  # Eastern: the 6 AM poller job ends here on morning days
SPORTS = ("nba", "nfl")
# The refresh before grading: schedules, final scores and box scores (and the injury report)
MORNING_JOBS = ("nba-scoreboard", "nba-boxscores", "nba-injuries", "nfl-schedules",
                "nfl-injuries")  # fmt: skip
STEPS = ("grade", "ledger", "engine", "select", "publish")
STATE = "morning:{day}"


def _eastern(now: datetime) -> datetime:
    from betdata.alerts.rules import EASTERN

    return now.astimezone(EASTERN)


def run_at(day: date) -> datetime:
    from betdata.alerts.rules import EASTERN

    return datetime.combine(day, AT, EASTERN)


def sports_due(main, now: datetime) -> list[str]:
    """The sports with games (not postponed or cancelled) yesterday or today (Eastern)."""
    from betdata.alerts.card import day_bounds
    from betdata.db.schema import games, sports

    today = _eastern(now).date()
    lo, _ = day_bounds(today - timedelta(days=1))
    _, hi = day_bounds(today)
    out = []
    with main.connect() as m:
        for sp in SPORTS:
            hit = m.execute(select(games.c.game_id)
                            .join(sports, sports.c.sport_id == games.c.sport_id)
                            .where(sports.c.key == sp, games.c.start_time_utc >= lo,
                                   games.c.start_time_utc < hi,
                                   games.c.status.not_in(("postponed", "cancelled")))
                            .limit(1)).first()  # fmt: skip
            if hit is not None:
                out.append(sp)
    return out


def poller_until(main, now: datetime, default: datetime) -> datetime:
    """The 6 AM ET poller job stops at 6:12 on morning days (the sweep has run 6:00-6:10)."""
    from betdata.alerts.rules import EASTERN

    t = _eastern(now)
    if t.hour != 6 or t.time() >= POLLER_STOP:
        return default
    try:
        due = sports_due(main, now)
    except Exception:  # noqa: BLE001 - never stops the poller
        return default
    stop = datetime.combine(t.date(), POLLER_STOP, EASTERN)
    return min(default, stop) if due else default


def stored_quotes(p, gid: int, now: datetime) -> list:
    """The latest archived quote per (book, market, player, side) of a game (the poller's
    sweeps store lines change-only, so the latest row is the line now)."""
    from betdata.alerts.db import quotes
    from betdata.alerts.feeds import Quote
    from betdata.alerts.grade import QUOTE_SOURCES, utc

    q = quotes
    latest: dict = {}
    for r in p.execute(select(q).where(q.c.game_id == gid, q.c.polled_at <= now,
                                       q.c.source.in_(QUOTE_SOURCES))
                       .order_by(q.c.polled_at, q.c.quote_id)):  # fmt: skip
        latest[(r.book, r.market, r.player_name, r.outcome)] = r
    return [Quote(r.sport, r.game_id, r.source, r.book, r.market, r.outcome, r.line, r.price,
                  r.player_name, utc(r.polled_at)) for r in latest.values()]


def today_games(main, sport: str, now: datetime) -> list[int]:
    """The sport's games today (Eastern) that have not started."""
    from betdata.alerts.card import day_bounds
    from betdata.db.schema import games, sports

    lo, hi = day_bounds(_eastern(now).date())
    with main.connect() as m:
        return list(m.execute(select(games.c.game_id)
                              .join(sports, sports.c.sport_id == games.c.sport_id)
                              .where(sports.c.key == sport, games.c.start_time_utc >= lo,
                                     games.c.start_time_utc < hi,
                                     games.c.start_time_utc > now,
                                     games.c.status.not_in(("postponed", "cancelled")))
                              .order_by(games.c.start_time_utc)).scalars())  # fmt: skip


@dataclass
class Report:
    day: str
    sports: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    refresh: list = field(default_factory=list)
    engine_picks: int = 0
    cards: dict = field(default_factory=dict)  # sport -> picks chosen (None: not chosen yet)
    shadow: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


def _step(rep: Report, name: str, fn, *args):
    rep.steps.append(name)
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 - one step's failure never stops the next
        log.exception("morning %s failed", name)
        rep.errors.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
        return None


def refresh(main, settings, registry, active, now: datetime) -> list[dict]:
    from betdata.runner.hourly import run_job

    out = []
    for sport in active:
        for job in sport.jobs:
            if job.id in MORNING_JOBS:
                out.append({"sport": sport.key, **run_job(main, settings, registry, job, now,
                                                          None)})  # fmt: skip
    return out


def grade(poller, now: datetime) -> None:
    poller.grade(now)


def ledger_sync(pdb, main, out: Path, now: datetime) -> dict:
    from betdata.ledger import sync

    with pdb.connect() as p, main.connect() as m:
        return sync(p, m, out, now)


def engine(poller, main, active, now: datetime) -> int:
    """The engine on today's NBA games with the archived lines (a fresh simulation per game)."""
    nba = next((s for s in active if s.key == "nba"), None)
    if nba is not None and hasattr(nba, "predict_engine"):
        nba.predict_engine(main, now)  # the live document (and the pre-tip snapshots)
    n = 0
    for gid in today_games(main, "nba", now):
        with poller.pdb.connect() as p:
            qs = stored_quotes(p, gid, now)
        n += len(poller.engine_picks(gid, qs, now, "morning") or [])
    return n


def select_cards(pdb, main, now: datetime) -> tuple[list, list]:
    from betdata.alerts import card as CD

    return CD.run(pdb, main, now), CD.run_shadow(pdb, main, now)


def run(main, pdb, poller, now: datetime, *, settings=None, registry=None, active=(),
        out: Path | None = None, build=None) -> Report:
    """The morning sequence (module docstring), a. to e.; `build()` builds the page."""
    from betdata.alerts.db import set_state
    from betdata.ledger import ledger_dir

    out = out or ledger_dir()
    day = _eastern(now).date()
    rep = Report(day.isoformat())
    rep.sports = sports_due(main, now)
    if not rep.sports:
        return rep

    def _grade():  # a. the refresh (final scores, box scores), then the grading
        if settings is not None:
            rep.refresh = refresh(main, settings, registry, active, now)
        grade(poller, now)

    _step(rep, "grade", _grade)
    _step(rep, "ledger", ledger_sync, pdb, main, out, now)
    rep.engine_picks = _step(rep, "engine", engine, poller, main, active, now) or 0
    got = _step(rep, "select", select_cards, pdb, main, now) or ([], [])
    cards, shadows = got
    rep.cards = {c["sport"]: len(c["picks"]) for c in cards}
    rep.shadow = {c["sport"]: len(c["picks"]) for c in shadows}
    _step(rep, "publish", lambda: (ledger_sync(pdb, main, out, now), build and build()))
    with pdb.begin() as p:  # the push reads this (and so does the verification)
        set_state(p, STATE.format(day=day.isoformat()),
                  {"at": now.isoformat(), "sports": rep.sports, "steps": rep.steps,
                   "cards": rep.cards, "shadow": rep.shadow, "errors": rep.errors,
                   "engine_picks": rep.engine_picks}, now)  # fmt: skip
    return rep


# --- the one push per sport ----------------------------------------------------------------------


def _units(u: float) -> str:
    return f"{u:+.1f}u" if abs(u) >= 0.05 or u == 0 else f"{u:+.2f}u"


def _wl(rec) -> str:
    return f"{rec.wins}-{rec.losses}" + (f"-{rec.pushes}" if rec.pushes else "")


def ungraded_games(rows: list[dict], sport: str, day: date) -> list[str]:
    """Games of the sport on `day` (Eastern) that still have an ungraded pick."""
    from betdata import official as O

    d = day.isoformat()
    return sorted({r["game"] for r in rows if r["sport"] == sport and not r.get("result")
                   and O._et_date(r["tip"]) == d})  # fmt: skip


def _pick_lines(p, card: dict, sport: str, today: date, signals, both: set) -> list[str]:
    from betdata import cards as C
    from betdata.alerts import card as CD
    from betdata.alerts.notify import BOOKS, _fmt_price, selection
    from betdata.alerts.rules import EASTERN

    by_key = {a.dedup_key: a for a in CD.day_picks(p, sport, today, signals)}
    out = []
    for x in card["picks"]:
        a = by_key.get(x["dedup_key"])
        if a is None:
            continue
        when = a.tip.astimezone(EASTERN).strftime("%-I:%M %p ET")
        mark = (" (on both cards)"
                if C.bet_key(a.game_id, a.market, a.player_name, a.side) in both else "")
        out.append(f"  {a.game_label} {when}: {selection(a)} {_fmt_price(a.price)} at "
                   f"{BOOKS.get(a.book, a.book)}, {x['label']} {x['stake']:g}u{mark}")
    return out


def _card_bets(p, card: dict | None, sport: str, today: date, signals) -> set[str]:
    from betdata import cards as C
    from betdata.alerts import card as CD

    if not card:
        return set()
    by_key = {a.dedup_key: a for a in CD.day_picks(p, sport, today, signals)}
    return {C.bet_key(a.game_id, a.market, a.player_name, a.side)
            for x in card["picks"] if (a := by_key.get(x["dedup_key"])) is not None}


def nba_message(p, rows: list[dict], now: datetime) -> tuple[str, str]:
    """The NBA morning push: both official cards, clearly separated (owner 2026-11-05).

    NBA · Fri Nov 6
    OG: last night 3-2, +0.6u · record 14-11, +3.3u (25 of 500)
    Today: 4 picks —
      ORL @ MIA 7:30 PM ET: Bam Adebayo over 9.5 rebounds +140 at FanDuel, LEAN 0.5u
    Next Gen: last night 0-0, +0.0u · record 0-0, +0.0u (0 of 500)
    Today: No picks today — the engine found no value
    Not graded yet: LAL @ POR (no final box score yet; graded on the next run)"""
    from betdata import cards as C
    from betdata import official as O
    from betdata.alerts import card as CD
    from betdata.alerts.db import get_state
    from betdata.alerts.rules import EASTERN

    today = now.astimezone(EASTERN).date()
    yday = (today - timedelta(days=1)).isoformat()
    state = {C.NEXTGEN: (get_state(p, CD.key("nba", today)), None),
             C.OG: (get_state(p, CD.shadow_key("nba", today)), CD.V1_SIGNALS)}
    both = (_card_bets(p, state[C.NEXTGEN][0], "nba", today, None)
            & _card_bets(p, state[C.OG][0], "nba", today, CD.V1_SIGNALS))
    lines = [f"NBA · {today.strftime('%a %b %-d')}"]
    for k in (C.OG, C.NEXTGEN):
        view = C.view(rows, k)
        last = O.record([r for r in view if r["day"] == yday], O.official_stake)
        rec, g = O.record(view, O.official_stake), O.goal(view)
        lines.append(f"{C.name(k)}: last night {_wl(last)}, {_units(last.units)} · record "
                     f"{_wl(rec)}, {_units(rec.units)} ({g.graded} of {O.GOAL_N})")
        card, signals = state[k]
        if not C.active(k):
            lines.append("Today: retired (no new card)")
        elif card is None:
            lines.append("Today: no lines posted yet for today's games — chosen at the first "
                         "later poll with lines")
        else:
            n, note = len(card["picks"]), CD.card_note(card)
            if n == 0:
                lines.append(f"Today: {note or 'no picks'}")
            else:
                lines.append(f"Today: {n} pick{'s' if n != 1 else ''} —")
                lines += _pick_lines(p, card, "nba", today, signals, both)
                if note:
                    lines.append(f"  {note}")
    late = ungraded_games(rows, "nba", today - timedelta(days=1))
    if late:
        lines.append(f"Not graded yet: {', '.join(late)} (no final box score yet; graded on "
                     "the next run)")
    return f"Morning update - NBA - {today.strftime('%a %b %-d')}", "\n".join(lines)


def message(p, rows: list[dict], sport: str, now: datetime) -> tuple[str, str] | None:
    """(title, body) of the sport's morning push. NBA: `nba_message` (both cards). NFL:
    'NFL · Last night: 3-2, +0.6u · Card 12-9, +1.8u (21 of 500) · Today's card (4 picks):
    ...', a line per card pick, then any game still ungraded."""
    if sport == "nba":
        return nba_message(p, rows, now)
    from betdata import official as O
    from betdata.alerts import card as CD
    from betdata.alerts.db import get_state
    from betdata.alerts.notify import BOOKS, _fmt_price, selection
    from betdata.alerts.rules import EASTERN

    today = now.astimezone(EASTERN).date()
    yday = today - timedelta(days=1)
    off = O.official(rows, sport)
    last = O.record([r for r in off if r["day"] == yday.isoformat()], O.official_stake)
    rec, g = O.record(off, O.official_stake), O.goal(off)
    parts = [f"{sport.upper()} · Last night: " + (f"{_wl(last)}, {_units(last.units)}"
                                                    if last.graded else "no graded card picks"),
             f"Card {_wl(rec)}, {_units(rec.units)} ({g.graded} of {O.GOAL_N})"]
    card = get_state(p, CD.key(sport, today))
    lines = []
    if card is None:
        parts.append("Today's card: no Live picks yet — chosen at the first later poll with one")
    else:
        n = len(card["picks"])
        parts.append(f"Today's card ({n} pick{'s' if n != 1 else ''}):")
        by_key = {a.dedup_key: a for a in CD.day_picks(p, sport, today)}
        for x in card["picks"]:
            a = by_key.get(x["dedup_key"])
            if a is None:
                continue
            when = a.tip.astimezone(EASTERN).strftime("%-I:%M %p ET")
            lines.append(f"{a.game_label} {when}: {selection(a)} {_fmt_price(a.price)} at "
                         f"{BOOKS.get(a.book, a.book)}, {x['label']} {x['stake']:g}u")
    late = ungraded_games(rows, sport, yday)
    if late:
        lines.append(f"Not graded yet: {', '.join(late)} (no final box score yet; graded on "
                     "the next run)")
    title = f"Morning update - {sport.upper()} - {today.strftime('%a %b %-d')}"
    return title, " · ".join(parts) + ("\n" + "\n".join(lines) if lines else "")


def push(pdb, out: Path, now: datetime, topic: str | None, client=None) -> dict[str, str]:
    """ONE ntfy message per sport with games yesterday or today; recorded on the morning state
    and on each card (`push`), so a card is never pushed twice."""
    import httpx

    from betdata import official as O
    from betdata.alerts import card as CD
    from betdata.alerts.db import get_state, set_state
    from betdata.alerts.notify import NTFY, PAGE
    from betdata.alerts.rules import EASTERN

    day = now.astimezone(EASTERN).date()
    with pdb.connect() as p:
        st = get_state(p, STATE.format(day=day.isoformat())) or {}
    rows = O.dataset(out)
    sent: dict[str, str] = {}
    for sport in st.get("sports", []):
        with pdb.connect() as p:
            msg = message(p, rows, sport, now)
        if msg is None:
            continue
        title, body = msg
        if not topic:
            status = "no NTFY_TOPIC"
        else:
            try:
                r = (client or httpx.Client(timeout=15)).post(
                    NTFY + topic, content=body.encode(),
                    headers={"Title": title, "Click": PAGE, "Tags": "sunrise"})
                status = f"http {r.status_code}"
            except Exception as e:  # noqa: BLE001 - a failed push never loses the update
                status = f"error {type(e).__name__}"
        sent[sport] = status
        with pdb.begin() as p:  # both NBA cards were in this push: never pushed again
            for k in (CD.key(sport, day), CD.shadow_key(sport, day)):
                c = get_state(p, k)
                if c is not None and "push" not in c:
                    set_state(p, k, {**c, "push": f"morning {status}"}, now)
    with pdb.begin() as p:
        st = get_state(p, STATE.format(day=day.isoformat())) or {}
        set_state(p, STATE.format(day=day.isoformat()), {**st, "push": sent}, now)
    return sent
