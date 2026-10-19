from datetime import UTC, datetime

import pytest

from betdata.nfl.points import EASTERN, final_report_time, point_time, prekick_time


def et(*a):
    return datetime(*a, tzinfo=EASTERN).astimezone(UTC)


@pytest.mark.parametrize(
    "kickoff, report",
    [
        (et(2024, 10, 6, 13, 0), et(2024, 10, 4, 17, 0)),  # Sunday -> Friday
        (et(2024, 10, 6, 9, 30), et(2024, 10, 4, 17, 0)),  # London Sunday morning -> Friday
        (et(2024, 10, 7, 20, 15), et(2024, 10, 5, 17, 0)),  # Monday -> Saturday
        (et(2024, 10, 10, 20, 15), et(2024, 10, 9, 17, 0)),  # Thursday -> Wednesday
        (et(2024, 12, 21, 16, 30), et(2024, 12, 20, 17, 0)),  # Saturday -> Friday
        (et(2024, 12, 25, 13, 0), et(2024, 12, 24, 17, 0)),  # Christmas Wednesday -> Tuesday
    ],
)
def test_final_report_time(kickoff, report):
    assert final_report_time(kickoff) == report


def test_prekick_is_75_minutes_before():
    k = et(2024, 10, 6, 13, 0)
    assert (k - prekick_time(k)).total_seconds() == 75 * 60
    assert point_time("prekick", k) < k and point_time("final_report", k) < point_time("prekick", k)
    with pytest.raises(ValueError):
        point_time("closing", k)
