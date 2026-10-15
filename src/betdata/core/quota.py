"""Free-tier budget enforcement.

Usage is counted locally from `api_usage` and reconciled with whatever the API reports
(e.g. The Odds API's x-requests-remaining). The higher of the two "used" figures wins, so
drift can only make us more conservative.

Two checks before every call:
  * hard cap: never spend past limit * (1 - safety_margin) in the period
  * pacing (priority="normal" only): spread what is left evenly over the rest of the period,
    so an early-month burst cannot starve later weeks. priority="high" skips pacing only.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, func, select

from betdata.config import QuotaPolicy
from betdata.core.errors import QuotaExceeded
from betdata.db.schema import api_quota, api_usage, utcnow


def period_bounds(policy: QuotaPolicy, now: datetime) -> tuple[datetime, datetime]:
    now = now.astimezone(UTC)
    if policy.period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if policy.period != "month":
        raise ValueError(f"unsupported quota period {policy.period!r}")
    day = min(policy.reset_day, 28)
    start = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    if now < start:
        start = _add_months(start, -1)
    return start, _add_months(start, 1)


def _add_months(dt: datetime, months: int) -> datetime:
    y, m = divmod(dt.month - 1 + months, 12)
    return dt.replace(year=dt.year + y, month=m + 1)


@dataclass(frozen=True)
class QuotaStatus:
    source: str
    limit: int
    usable: int
    used_local: int
    used_effective: int
    remaining_reported: int | None
    remaining_usable: int
    used_today: int
    daily_allowance: int
    period_start: datetime
    period_end: datetime


class QuotaManager:
    def __init__(
        self,
        engine: Engine,
        source: str,
        policy: QuotaPolicy,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.engine = engine
        self.source = source
        self.policy = policy
        self.clock = clock

    def status(self) -> QuotaStatus:
        now = self.clock()
        start, end = period_bounds(self.policy, now)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.engine.connect() as conn:
            used_local = self._sum_usage(conn, start)
            used_today = self._sum_usage(conn, max(start, day_start))
            reported = conn.execute(
                select(api_quota.c.remaining_reported).where(
                    api_quota.c.source == self.source, api_quota.c.period_start == start
                )
            ).scalar()

        used_effective = used_local
        if reported is not None:
            used_effective = max(used_local, self.policy.limit - reported)
        usable = math.floor(self.policy.limit * (1 - self.policy.safety_margin))
        remaining_usable = max(0, usable - used_effective)
        days_left = max(1, math.ceil((end - day_start).total_seconds() / 86400))
        daily_allowance = math.floor((remaining_usable + used_today) / days_left)
        return QuotaStatus(
            source=self.source,
            limit=self.policy.limit,
            usable=usable,
            used_local=used_local,
            used_effective=used_effective,
            remaining_reported=reported,
            remaining_usable=remaining_usable,
            used_today=used_today,
            daily_allowance=daily_allowance,
            period_start=start,
            period_end=end,
        )

    def check(self, cost: int, priority: str = "normal") -> QuotaStatus:
        """Raises QuotaExceeded if a call costing `cost` units must not be made now."""
        s = self.status()
        if cost > s.remaining_usable:
            raise QuotaExceeded(
                f"{self.source}: call costs {cost}, only {s.remaining_usable} usable left "
                f"this period (limit {s.limit}, margin {self.policy.safety_margin:.0%})"
            )
        if priority == "normal" and s.used_today + cost > s.daily_allowance:
            raise QuotaExceeded(
                f"{self.source}: paced allowance reached ({s.used_today}+{cost} > "
                f"{s.daily_allowance}/day); use priority=high to override pacing"
            )
        return s

    def reconcile(self, remaining: int | None, used: int | None) -> None:
        if remaining is None and used is None:
            return
        now = self.clock()
        start, end = period_bounds(self.policy, now)
        values = dict(remaining_reported=remaining, used_reported=used, last_reconciled=now)
        with self.engine.begin() as conn:
            updated = conn.execute(
                api_quota.update()
                .where(api_quota.c.source == self.source, api_quota.c.period_start == start)
                .values(**values)
            ).rowcount
            if not updated:
                conn.execute(
                    api_quota.insert().values(
                        source=self.source,
                        period_start=start,
                        period_end=end,
                        limit_units=self.policy.limit,
                        **values,
                    )
                )

    def _sum_usage(self, conn, since: datetime) -> int:
        return conn.execute(
            select(func.coalesce(func.sum(api_usage.c.cost_units), 0)).where(
                api_usage.c.source == self.source, api_usage.c.called_at >= since
            )
        ).scalar_one()
