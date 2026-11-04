"""Plain statuses and when results post (owner request 2026-11-04; alerts/tracker.py)."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from betdata.alerts.tracker import grading_eta, plain_status
from betdata.page.official import _result_cell

ET = ZoneInfo("America/New_York")


def _et(h, m=0, day=4):
    return datetime(2026, 11, day, h, m, tzinfo=ET).astimezone(UTC)


def test_result_posts_on_the_next_hourly_run_after_the_expected_end():
    e = grading_eta(_et(19), "nba")  # ends ~9:30 PM -> the 10 PM run
    assert e["grade_text"] == "grading by ~10:15 PM ET"
    assert datetime.fromisoformat(e["grade_at"]) == _et(22, 15)
    e = grading_eta(_et(18, 30), "nba")  # ends exactly 9:00 PM -> the 9 PM run
    assert e["grade_text"] == "grading by ~9:15 PM ET"


def test_after_the_last_run_of_the_day_results_post_tomorrow_morning():
    for tip in (_et(19, 31), _et(22), _et(20, 0)):
        e = grading_eta(tip, "nba")
        assert e["grade_at"] is None and e["grade_text"] == "grading tomorrow ~9:00 AM ET"
    assert grading_eta(_et(13), "nfl")["grade_text"] == "grading by ~5:15 PM ET"  # 3h15m


def test_plain_status_before_during_after():
    tip = _et(19)
    assert plain_status(tip, "nba", tip - timedelta(hours=3)) == "Tips 7:00 PM ET"
    assert plain_status(tip, "nba", tip + timedelta(minutes=40)) == "In progress"
    assert plain_status(tip, "nba", tip + timedelta(minutes=40), score="X 50-48 Y") == \
        "In progress X 50-48 Y"
    assert plain_status(tip, "nba", tip + timedelta(hours=2, minutes=40)) == \
        "Final — grading by ~10:15 PM ET"
    assert plain_status(tip, "nba", tip, final=True).startswith("Final — ")


def test_the_word_pending_is_gone_from_result_cells():
    r = {"result": "", "tip": _et(19).isoformat(), "sport": "nba"}
    cell = _result_cell(r, _et(12))
    assert "pending" not in cell and "Tips 7:00 PM ET" in cell
