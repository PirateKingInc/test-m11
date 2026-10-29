"""The lag study (MODEL.md "M16 / Lag study", Part E): how fast DraftKings, FanDuel and Pinnacle
move after injury news, and the go/no-go rule for Signal 1.

Per qualifying status change and book:
- **publication**: the report's stated time (else when the poller first read it);
- **baseline**: the book's last price known before publication (poller polls, else the hourly
  job's SharpAPI / The Odds API polls in the main database);
- **lag**: minutes from publication to the first poll within 60 minutes whose spread or total
  line moved by >= 0.5 points or whose moneyline no-vig moved by >= 1.5 pp versus the baseline,
  else censored at 60. A move already there at the first poll counts at that poll's time.

Rule (fixed): once the study has run two weeks of live games, if the median soft-book lag (DK
and FD pooled) is under 5 minutes, Signal 1 is switched off and logged as not viable on free
data. The decision is taken once and stored in the poller database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median

from sqlalchemy import Connection, select

from betdata.alerts import rules as R
from betdata.alerts.db import get_state, quotes, set_state, status_events
from betdata.db.schema import books, markets, odds_snapshots
from betdata.moneyline import no_vig

BOOKS = ("draftkings", "fanduel", "pinnacle")
SOFT = ("draftkings", "fanduel")
MAIN_MARKETS = ("spreads", "totals", "h2h")
STUDY_DAYS = 14


@dataclass
class Lag:
    event_id: int
    sport: str
    game_id: int
    player: str
    change: str
    published: datetime
    book: str
    minutes: float | None  # None: no baseline or no poll for this book
    censored: bool
    market: str | None  # the market that moved first


def _utc(t):
    from datetime import UTC

    return t if t is None or t.tzinfo else t.replace(tzinfo=UTC)


def _snapshot(rows) -> dict:
    """market -> (line, no-vig of home/over) from one book's rows of one poll."""
    by: dict = {}
    for r in rows:
        by.setdefault(r["market"], {})[r["outcome"]] = (r["line"], r["price"])
    out = {}
    for m, d in by.items():
        a, b = (d.get("over"), d.get("under")) if m == "totals" else (d.get("home"), d.get("away"))
        if a is None or b is None or a[1] is None or b[1] is None:
            continue
        nv = no_vig(int(a[1]), int(b[1]))
        out[m] = (a[0], nv[0] if nv else None)
    return out


def _main_baseline(main: Connection, game_id: int, book: str, before: datetime) -> dict:
    """The hourly job's last stored main lines for a book before a time."""
    rows = main.execute(
        select(markets.c.key.label("market"), odds_snapshots.c.outcome,
               odds_snapshots.c.line, odds_snapshots.c.price_american.label("price"),
               odds_snapshots.c.fetched_at)
        .join(markets, markets.c.market_id == odds_snapshots.c.market_id)
        .join(books, books.c.book_id == odds_snapshots.c.book_id)
        .where(odds_snapshots.c.game_id == game_id, books.c.key == book,
               odds_snapshots.c.snapshot_kind == "poll", odds_snapshots.c.price_status == "active",
               odds_snapshots.c.fetched_at < before, markets.c.period == "game",
               markets.c.key.in_(MAIN_MARKETS))
        .order_by(odds_snapshots.c.fetched_at)).mappings().all()  # fmt: skip
    last: dict = {}
    for r in rows:  # change-only rows: the latest per (market, outcome) is the line then
        last[(r["market"], r["outcome"])] = dict(r)
    return _snapshot(last.values())


def event_lags(p: Connection, main: Connection | None, ev) -> list[Lag]:
    published = _utc(ev.report_at) or _utc(ev.detected_at)
    end = published + timedelta(minutes=R.LAG_WINDOW_MIN)
    rows = p.execute(select(quotes.c.polled_at, quotes.c.book, quotes.c.market, quotes.c.outcome,
                            quotes.c.line, quotes.c.price)
                     .where(quotes.c.game_id == ev.game_id, quotes.c.player_name.is_(None),
                            quotes.c.price.is_not(None),
                            quotes.c.market.in_(MAIN_MARKETS),
                            quotes.c.polled_at <= end)
                     .order_by(quotes.c.polled_at)).mappings().all()  # fmt: skip
    out = []
    for book in BOOKS:
        polls: dict = {}
        for r in rows:
            if r["book"] == book:
                polls.setdefault(_utc(r["polled_at"]), []).append(r)
        before = [t for t in polls if t < published]
        base = _snapshot(polls[max(before)]) if before else {}
        if not base and main is not None:
            base = _main_baseline(main, ev.game_id, book, published)
        after = sorted(t for t in polls if t >= published)
        label = f"{ev.old_status or 'not listed'} -> {ev.new_status or 'not listed'}"
        if not base or not after:
            out.append(Lag(ev.event_id, ev.sport, ev.game_id, ev.player_name, label, published,
                           book, None, False, None))  # fmt: skip
            continue
        hit = None
        for t in after:
            snap = _snapshot(polls[t])
            for m in MAIN_MARKETS:
                if m in snap and m in base and R.moved(m, base[m][0], base[m][1], snap[m][0],
                                                       snap[m][1]):  # fmt: skip
                    hit = (t, m)
                    break
            if hit:
                break
        if hit:
            out.append(Lag(ev.event_id, ev.sport, ev.game_id, ev.player_name, label, published,
                           book, round((hit[0] - published).total_seconds() / 60, 1), False,
                           hit[1]))  # fmt: skip
        else:
            out.append(Lag(ev.event_id, ev.sport, ev.game_id, ev.player_name, label, published,
                           book, float(R.LAG_WINDOW_MIN), True, None))  # fmt: skip
    return out


def all_lags(p: Connection, main: Connection | None, now: datetime) -> list[Lag]:
    """Lags of every qualifying change whose 60-minute window has closed."""
    evs = p.execute(select(status_events).where(status_events.c.qualifies.is_(True))
                    .order_by(status_events.c.detected_at)).all()  # fmt: skip
    out = []
    for ev in evs:
        published = _utc(ev.report_at) or _utc(ev.detected_at)
        if published + timedelta(minutes=R.LAG_WINDOW_MIN) <= now:
            out += event_lags(p, main, ev)
    return out


def summary(lags: list[Lag]) -> dict:
    by_book = {}
    for b in BOOKS:
        xs = [x.minutes for x in lags if x.book == b and x.minutes is not None]
        by_book[b] = {"events": len(xs), "median": median(xs) if xs else None,
                      "censored": sum(1 for x in lags if x.book == b and x.censored),
                      "under_5": sum(1 for x in xs if x < R.LAG_RULE_MIN)}  # fmt: skip
    soft = [x.minutes for x in lags if x.book in SOFT and x.minutes is not None]
    return {"by_book": by_book, "soft_n": len(soft),
            "soft_median": median(soft) if soft else None}  # fmt: skip


def study_start(p: Connection):
    first = p.execute(select(status_events.c.detected_at).where(
        status_events.c.qualifies.is_(True)).order_by(status_events.c.detected_at)).first()
    return _utc(first[0]) if first else None


def decide(p: Connection, main: Connection | None, now: datetime) -> dict | None:
    """Take the Signal 1 decision once, two weeks after the first qualifying change."""
    if get_state(p, "signal1:decision") is not None:
        return get_state(p, "signal1:decision")
    start = study_start(p)
    if start is None or now < start + timedelta(days=STUDY_DAYS):
        return None
    s = summary(all_lags(p, main, now))
    off = s["soft_median"] is not None and s["soft_median"] < R.LAG_RULE_MIN
    decision = {"decided_at": now.isoformat(), "study_start": start.isoformat(),
                "soft_events": s["soft_n"], "soft_median_min": s["soft_median"],
                "signal1": "off: not viable on free data" if off else "on",
                "by_book": s["by_book"]}  # fmt: skip
    set_state(p, "signal1:decision", decision, now)
    if off:
        set_state(p, "signal1:off", True, now)
    return decision


NFL_STUDY_DAYS = 28  # M18 Part C: a 4-week NFL lag study
NFL_STUDY_FROM = datetime(2026, 10, 29, tzinfo=UTC)  # M18 start: NFL changes from here count


def nfl_study_start(p: Connection):
    first = p.execute(select(status_events.c.detected_at).where(
        status_events.c.qualifies.is_(True), status_events.c.sport == "nfl",
        status_events.c.detected_at >= NFL_STUDY_FROM)
        .order_by(status_events.c.detected_at)).first()  # fmt: skip
    return _utc(first[0]) if first else None


def decide_nfl(p: Connection, main: Connection | None, now: datetime) -> dict | None:
    """M18: NFL value alerts go live only if, after four weeks, the pooled DK+FD median lag of
    NFL changes is at least 5 minutes (the M16 rule); taken once."""
    if get_state(p, "nfl_value:decision") is not None:
        return get_state(p, "nfl_value:decision")
    start = nfl_study_start(p)
    if start is None or now < start + timedelta(days=NFL_STUDY_DAYS):
        return None
    lags = [x for x in all_lags(p, main, now)
            if x.sport == "nfl" and x.published >= NFL_STUDY_FROM]  # fmt: skip
    s = summary(lags)
    off = s["soft_median"] is None or s["soft_median"] < R.LAG_RULE_MIN
    decision = {"decided_at": now.isoformat(), "study_start": start.isoformat(),
                "soft_events": s["soft_n"], "soft_median_min": s["soft_median"],
                "nfl_value": "off: not viable on free data" if off else "live",
                "by_book": s["by_book"]}  # fmt: skip
    set_state(p, "nfl_value:decision", decision, now)
    if off:
        set_state(p, "nfl_value:off", True, now)
    return decision


def report_lines(lags: list[Lag], decision: dict | None, start) -> list[str]:
    s = summary(lags)
    out = [f"lag study: {len({x.event_id for x in lags})} qualifying changes with a closed "
           f"60-minute window; study start {start:%Y-%m-%d %H:%M} UTC" if start else
           "lag study: no qualifying change yet"]  # fmt: skip
    for b, d in s["by_book"].items():
        med = f"{d['median']:.1f}" if d["median"] is not None else "-"
        out.append(f"  {b:11} events {d['events']:3}  median lag {med:>5} min  "
                   f"under 5 min {d['under_5']:3}  censored at 60 {d['censored']:3}")
    med = f"{s['soft_median']:.1f} min" if s["soft_median"] is not None else "-"
    out.append(f"  DK+FD pooled: {s['soft_n']} lags, median {med} (rule: < 5 min -> Signal 1 off)")
    out.append(f"  decision: {decision['signal1']} ({decision['decided_at'][:16]})" if decision
               else "  decision: pending (taken once, two weeks after the study start)")
    return out
