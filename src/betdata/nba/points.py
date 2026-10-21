"""The two instants every NBA game is predicted at (MODEL.md, M14 "Prediction points").

morning: 14:00 US Eastern on game day, the first hourly run after the first official injury
  report of game day (1:30 PM in the three-a-day era; teams file game-day statuses between
  11 a.m. and 1 p.m. local). For a tip before 15:00 ET, three hours before tip (not before
  07:00, the runner's first hour).
pretip: the last hourly run strictly before tip (runs are on the hour, 07:00-22:00 ET).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
MORNING = "morning"
PRETIP = "pretip"
POINTS = (MORNING, PRETIP)
MORNING_CLOCK = time(14, 0)
FIRST_RUN, LAST_RUN = 7, 22  # the hourly runner's first and last hour (Eastern)


def pretip_time(tip: datetime) -> datetime:
    local = (tip - timedelta(seconds=1)).astimezone(EASTERN)
    hour = local.replace(minute=0, second=0, microsecond=0)
    if hour.hour > LAST_RUN:
        hour = hour.replace(hour=LAST_RUN)
    elif hour.hour < FIRST_RUN:  # a tip before 07:00 ET: the previous evening's last run
        hour = (hour - timedelta(days=1)).replace(hour=LAST_RUN)
    return hour.astimezone(tip.tzinfo)


def morning_time(tip: datetime) -> datetime:
    local = tip.astimezone(EASTERN)
    out = datetime.combine(local.date(), MORNING_CLOCK, tzinfo=EASTERN)
    if local.hour < 15:
        early = (local - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
        out = max(early, datetime.combine(local.date(), time(FIRST_RUN), tzinfo=EASTERN))
    return min(out.astimezone(tip.tzinfo), pretip_time(tip))


def point_time(point: str, tip: datetime) -> datetime:
    if point == MORNING:
        return morning_time(tip)
    if point == PRETIP:
        return pretip_time(tip)
    raise ValueError(f"unknown prediction point {point!r}; expected one of {POINTS}")
