"""The two instants every NFL game is predicted at.

final_report: 17:00 US Eastern on the day of the last official injury report. The NFL's final
  report comes two days before Sunday and Monday games (Friday, Saturday) and the day before a
  game on any other day (Wednesday for Thursday games, Friday for Saturday games).
prekick: 75 minutes before kickoff, after game-day inactives (announced 90 minutes before).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
FINAL_REPORT = "final_report"
PREKICK = "prekick"
POINTS = (FINAL_REPORT, PREKICK)
FINAL_REPORT_CLOCK = time(17, 0)
PREKICK_LEAD = timedelta(minutes=75)


def final_report_time(kickoff: datetime) -> datetime:
    local = kickoff.astimezone(EASTERN)
    days_before = 2 if local.weekday() in (6, 0) else 1  # Sunday, Monday
    day = local.date() - timedelta(days=days_before)
    out = datetime.combine(day, FINAL_REPORT_CLOCK, tzinfo=EASTERN).astimezone(kickoff.tzinfo)
    return min(out, kickoff - PREKICK_LEAD)


def prekick_time(kickoff: datetime) -> datetime:
    return kickoff - PREKICK_LEAD


def point_time(point: str, kickoff: datetime) -> datetime:
    if point == FINAL_REPORT:
        return final_report_time(kickoff)
    if point == PREKICK:
        return prekick_time(kickoff)
    raise ValueError(f"unknown prediction point {point!r}; expected one of {POINTS}")
