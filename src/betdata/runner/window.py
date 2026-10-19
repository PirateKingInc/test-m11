"""When the hourly runner works: every hour on the hour, 7:00am to 10:00pm America/New_York
(16 runs a day), in any season and across daylight-saving changes.

GitHub's cron is UTC only. Eastern is UTC-4 in summer (7am = 11:00 UTC, 10pm = 02:00 UTC) and
UTC-5 in winter (7am = 12:00 UTC, 10pm = 03:00 UTC), so the workflow fires every hour of the
union, 11:00-03:00 UTC (17 times a day), and each run first asks `in_window` whether its hour is
7am-10pm Eastern; the one extra hour a day exits at once. GitHub often starts scheduled runs late,
so a run belongs to the hour it was due in (the UTC hour it started in), not its start minute.

Standard library only: the workflow runs this before installing anything, so an off-hours run
costs a few seconds:  python3 src/betdata/runner/window.py  ->  run=true|false
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
FIRST_HOUR, LAST_HOUR = 7, 22  # Eastern, inclusive: 7am ... 10pm
CRON_UTC_HOURS = (0, 1, 2, 3, *range(11, 24))  # .github/workflows/hourly.yml
CRON = "0 0-3,11-23 * * *"


def slot(now: datetime) -> datetime:
    """The hourly slot a run belongs to: its start time floored to the hour, in UTC."""
    return now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def in_window(now: datetime) -> bool:
    return FIRST_HOUR <= slot(now).astimezone(EASTERN).hour <= LAST_HOUR


def cron_fires(day: date) -> list[datetime]:
    """Every UTC time the workflow's cron fires on this UTC calendar day."""
    return [datetime.combine(day, time(h), tzinfo=UTC) for h in sorted(CRON_UTC_HOURS)]


def runs_on(eastern_day: date) -> list[datetime]:
    """The Eastern times on this Eastern calendar day at which a scheduled run does work."""
    out = []
    for d in (eastern_day - timedelta(days=1), eastern_day, eastern_day + timedelta(days=1)):
        for t in cron_fires(d):
            local = t.astimezone(EASTERN)
            if local.date() == eastern_day and in_window(t):
                out.append(local)
    return sorted(out)


def main(argv: list[str]) -> int:
    now = datetime.fromisoformat(argv[1]) if len(argv) > 1 else datetime.now(UTC)
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    run = manual or in_window(now)
    line = f"run={'true' if run else 'false'}"
    print(f"{line} (slot {slot(now):%Y-%m-%d %H:%M} UTC = "
          f"{slot(now).astimezone(EASTERN):%H:%M %Z}{', manual' if manual else ''})")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
