"""Verification (e): a worst-case 30-day month of poller requests, on the busy schedule of the
hourly quota test (NBA games every day, a busy NFL week), stays inside every free limit."""

from datetime import UTC, datetime, time

from betdata.alerts.feeds import HOURLY_CREDITS_PER_DAY
from betdata.alerts.quota import project
from tests.runner.test_quota_month import START, _schedule


def test_a_worst_case_month_of_polling_fits_the_free_plans(engine):
    with engine.begin() as conn:
        _schedule(conn)
        pr = project(conn, datetime.combine(START, time(4), tzinfo=UTC), 30)
    assert pr.nba_game_days == 30 and pr.nfl_windows >= 20
    hourly = round(HOURLY_CREDITS_PER_DAY * 30)
    # The Odds API: the hourly job's ~159 + the poller's boards, inside 450 usable of 500.
    assert hourly + pr.requests["the_odds_api"] <= 450, pr.requests["the_odds_api"]
    lines = pr.lines(hourly)
    assert not [x for x in lines if "OVER BUDGET" in x]
    # SharpAPI's worst day: well under 10 calls a minute averaged over the polling hours.
    worst = max(d["sharpapi"] for d in pr.by_day.values())
    assert worst <= 10 * 60 * 12  # 12 polling hours a day at the spacing cap
