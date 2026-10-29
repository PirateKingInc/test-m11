"""The M16 poller's 30-day request projection (Part B5, verification e), on the stored schedule,
added to the hourly job's own projection for the sources they share.

Worst-case assumptions (each is an upper bound on what the poller does):
- NBA report window: one poll every 3 minutes, <= 2 requests a poll (an unpublished name, then
  the new report), 10:00-19:30 ET on game days and 16:30-20:30 ET the day before.
- NFL: every 5 minutes from 120 to 60 minutes before each kickoff window, 3 ESPN requests per
  game per poll (summary + two rosters).
- Lag windows: QUALIFYING_PER_DAY changes on an NBA game day and per NFL kickoff window, each
  polled for 60 minutes at its sport's cadence with 2 SharpAPI calls + 1 Kalshi call, never
  shared between changes of the same game (real use shares them).
- Signal 2: NBA two boards a game day, NFL one per kickoff window: 3 Pinnacle credits + 2
  SharpAPI calls each. Closing polls: 3 polls x 2 calls per game with an alert (all games).

SharpAPI's per-minute limit is held structurally: the poller spaces its calls 6 s apart (<= 10
a minute, limit 12) and never polls while an hourly run is queued or running.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Connection, select

from betdata.alerts.feeds import HOURLY_CREDITS_PER_DAY, ODDS_SAFETY
from betdata.alerts.rules import EASTERN
from betdata.core.matching import sport_id
from betdata.db.schema import games

QUALIFYING_PER_DAY = {"nba": 12, "nfl": 8}
SWEEPS_PER_DAY, SWEEP_CALLS = 18, 3
ODDS_LIMIT = 500


@dataclass
class Projection:
    days: int
    nba_game_days: int = 0
    nfl_windows: int = 0
    requests: Counter = field(default_factory=Counter)  # source -> requests (credits for odds)
    by_day: dict = field(default_factory=dict)  # date -> Counter

    def lines(self, hourly_odds: int | None = None) -> list[str]:
        h = hourly_odds if hourly_odds is not None else round(HOURLY_CREDITS_PER_DAY * self.days)
        pin = self.requests["the_odds_api"]
        usable = int(ODDS_LIMIT * 0.9)
        peak = {s: max((d[s] for d in self.by_day.values()), default=0) for s in self.requests}
        out = [f"M16 poller, next {self.days} days: {self.nba_game_days} NBA game days, "
               f"{self.nfl_windows} NFL kickoff windows (worst case)"]  # fmt: skip
        for s, n in sorted(self.requests.items()):
            unit = "credits" if s == "the_odds_api" else "requests"
            out.append(f"  {s:13} {n:7} {unit}  (peak day {peak[s]})")
        verdict = "OK" if h + pin <= usable else "OVER BUDGET"
        out.append(f"  The Odds API: hourly {h} + poller {pin} = {h + pin} of {usable} usable "
                   f"(500 less 10%) -> {verdict}; the poller also keeps a live reserve "
                   f"({HOURLY_CREDITS_PER_DAY:.1f}/day for the hourly job + {ODDS_SAFETY}) and "
                   f"skips a board that would touch it")  # fmt: skip
        out.append("  SharpAPI: <= 10 calls a minute by spacing (limit 12), none while an "
                   "hourly run is in progress; no daily cap on the free plan")
        out.append("  NBA report, ESPN, Kalshi: no quota; <= 1 request a second each")
        return out


def project(conn: Connection, start: datetime, days: int = 30) -> Projection:
    pr = Projection(days)
    end = start + timedelta(days=days)
    tips = {}
    for s in ("nba", "nfl"):
        rows = conn.execute(select(games.c.start_time_utc).where(
            games.c.sport_id == sport_id(conn, s), games.c.start_time_utc >= start,
            games.c.start_time_utc < end + timedelta(days=1),
            games.c.status.notin_(("postponed", "cancelled")))).scalars()  # fmt: skip
        tips[s] = sorted(_utc(t) for t in rows)
    nba_days = Counter(t.astimezone(EASTERN).date() for t in tips["nba"])
    first = start.astimezone(EASTERN).date()
    for i in range(days):
        d = first + timedelta(days=i)
        c = pr.by_day.setdefault(d, Counter())
        if d in nba_days:
            pr.nba_game_days += 1
            c["nba_injury"] += 2 * _cycles(time(10, 0), time(19, 30), 3)
            ch = QUALIFYING_PER_DAY["nba"]
            c["sharpapi"] += ch * 20 * 2 + 2 * 2 + nba_days[d] * 3 * 2
            c["kalshi"] += ch * 20
            c["the_odds_api"] += 2 * 3
        if d + timedelta(days=1) in nba_days:
            c["nba_injury"] += 2 * _cycles(time(16, 30), time(20, 30), 3)
    windows = Counter(t for t in tips["nfl"] if t < end)
    for kick, n in windows.items():
        d = kick.astimezone(EASTERN).date()
        if d not in pr.by_day:
            continue
        c = pr.by_day[d]
        pr.nfl_windows += 1
        c["espn"] += 8 * 3 * n  # M18: T-100..T-60, every 5 minutes
        ch = QUALIFYING_PER_DAY["nfl"]
        c["sharpapi"] += ch * 12 * 2 + 2 + n * 3 * 2
        c["kalshi"] += ch * 12
        c["the_odds_api"] += 3
    # M18 Part C: Fridays 16:00-20:00 ET, every 5 minutes, one ESPN summary per game of the
    # next four days.
    for d, c in pr.by_day.items():
        if d.weekday() == 4:
            fri = datetime.combine(d, time(16), EASTERN)
            n = sum(1 for t in tips["nfl"] if fri < t <= fri + timedelta(days=4))
            c["espn"] += 48 * n
    # M18 Part E sweeps: 18 a day (06:00, 07-23 hourly, 23:30 ET), each game with a tip in the
    # next 36 hours, SWEEP_CALLS SharpAPI calls a game (main lines + props, up to 3 pages).
    every = sorted(tips["nba"] + tips["nfl"])
    for d, c in pr.by_day.items():
        noon = datetime.combine(d, time(12), EASTERN)
        n = sum(1 for t in every if noon - timedelta(hours=6) < t <= noon + timedelta(hours=30))
        c["sharpapi"] += SWEEPS_PER_DAY * SWEEP_CALLS * n
    for c in pr.by_day.values():
        pr.requests.update(c)
    return pr


def _cycles(a: time, b: time, minutes: int) -> int:
    return (datetime.combine(date.min, b) - datetime.combine(date.min, a)).seconds // 60 // minutes


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)
