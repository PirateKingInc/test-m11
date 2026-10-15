from datetime import UTC, datetime

import pytest

from betdata.config import QuotaPolicy
from betdata.core.errors import QuotaExceeded
from betdata.core.quota import QuotaManager, period_bounds
from betdata.db.schema import api_usage

POLICY = QuotaPolicy(limit=500, period="month", reset_day=1, safety_margin=0.10)


def spend(engine, clock, units, source="the_odds_api"):
    with engine.begin() as conn:
        conn.execute(
            api_usage.insert().values(
                source=source, endpoint="/x", called_at=clock(), cost_units=units
            )
        )


def test_period_bounds_monthly_and_reset_day():
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    assert period_bounds(POLICY, now) == (
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 11, 1, tzinfo=UTC),
    )
    p = QuotaPolicy(limit=1, reset_day=20)
    assert period_bounds(p, now) == (
        datetime(2026, 9, 20, tzinfo=UTC),
        datetime(2026, 10, 20, tzinfo=UTC),
    )
    dec = datetime(2026, 12, 31, tzinfo=UTC)
    assert period_bounds(POLICY, dec)[1] == datetime(2027, 1, 1, tzinfo=UTC)


def test_hard_cap_holds_back_safety_margin(engine, clock):
    qm = QuotaManager(engine, "the_odds_api", POLICY, clock=clock)
    spend(engine, clock, 448)
    qm.check(2, priority="high")  # 450 usable
    with pytest.raises(QuotaExceeded, match="usable"):
        qm.check(3, priority="high")


def test_api_reported_remaining_overrides_lower_local_count(engine, clock):
    qm = QuotaManager(engine, "the_odds_api", POLICY, clock=clock)
    qm.reconcile(remaining=60, used=440)  # e.g. calls made from another machine
    s = qm.status()
    assert s.used_local == 0 and s.used_effective == 440 and s.remaining_usable == 10
    with pytest.raises(QuotaExceeded):
        qm.check(11, priority="high")


def test_pacing_limits_normal_priority_only(engine, clock):
    qm = QuotaManager(engine, "the_odds_api", POLICY, clock=clock)
    s = qm.status()  # Oct 15 -> Nov 1 = 17 days, 450 usable -> 26/day
    assert s.daily_allowance == 450 // 17
    spend(engine, clock, s.daily_allowance)
    with pytest.raises(QuotaExceeded, match="paced"):
        qm.check(3)
    qm.check(3, priority="high")
    clock.advance(days=1)
    qm.check(3)  # new day, new allowance


def test_usage_of_other_sources_is_ignored(engine, clock):
    qm = QuotaManager(engine, "the_odds_api", POLICY, clock=clock)
    spend(engine, clock, 1000, source="cfbd")
    assert qm.status().used_effective == 0
