"""APScheduler wiring, single-instance lock, and spend projection."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from filelock import FileLock, Timeout
from sqlalchemy import Engine

from betdata.config import PROJECT_ROOT, Settings
from betdata.core.quota import QuotaManager
from betdata.db.schema import utcnow
from betdata.scheduler.jobs import JobDef, Schedule, execute, gate_open, validate

log = logging.getLogger(__name__)
LOCK_PATH = PROJECT_ROOT / "data" / "scheduler.lock"
LOG_PATH = PROJECT_ROOT / "data" / "logs" / "scheduler.log"


class WallClockCronTrigger(BaseTrigger):
    """Cron on local wall-clock time, safe across DST.

    APScheduler 3's CronTrigger with a DST zone can loop forever at the fall-back hour (e.g.
    01:50 happens twice in New York on the first Sunday of November). Here the cron
    expression is evaluated on wall-clock time represented in UTC (no DST, so no ambiguity),
    and each result is converted back to the real zone:
      * repeated wall time (fall-back): fires once, at the first occurrence
      * missing wall time (spring-forward, e.g. 02:50): fires an hour later in real time,
        unless that instant already fired
    """

    def __init__(self, crontab: str, timezone: ZoneInfo):
        self.timezone = timezone
        self.crontab = crontab
        self._wall = CronTrigger.from_crontab(crontab, timezone=UTC)

    def _to_wall(self, dt: datetime) -> datetime:
        return dt.astimezone(self.timezone).replace(tzinfo=UTC)

    def get_next_fire_time(self, previous_fire_time, now):
        wall_prev = self._to_wall(previous_fire_time) if previous_fire_time else None
        wall_now = self._to_wall(now)
        for _ in range(10_000):
            wall = self._wall.get_next_fire_time(wall_prev, wall_now)
            if wall is None:
                return None
            # fold=0 picks the first occurrence; the UTC round-trip normalizes missing times.
            real = wall.replace(tzinfo=self.timezone).astimezone(UTC).astimezone(self.timezone)
            if real >= now and (previous_fire_time is None or real > previous_fire_time):
                return real
            wall_prev, wall_now = wall, wall + timedelta(seconds=1)
        raise RuntimeError(f"no next fire time found for {self.crontab!r}")

    def __str__(self) -> str:
        return f"cron[{self.crontab}] {self.timezone.key}"


def trigger_for(job: JobDef, schedule: Schedule) -> WallClockCronTrigger:
    return WallClockCronTrigger(job.cron, schedule.timezone)


def fire_times(trigger: BaseTrigger, start: datetime, end: datetime) -> Iterator[datetime]:
    t = trigger.get_next_fire_time(None, start)
    while t is not None and t < end:
        yield t
        t = trigger.get_next_fire_time(t, t + timedelta(seconds=1))


def build_scheduler(
    engine: Engine,
    settings: Settings,
    registry: dict,
    schedule: Schedule,
    scheduler_cls: type[BaseScheduler] = BlockingScheduler,
) -> BaseScheduler:
    # One worker: jobs run one at a time, so SQLite never sees concurrent writers.
    sched = scheduler_cls(executors={"default": ThreadPoolExecutor(1)}, timezone=schedule.timezone)
    for job in schedule.jobs:
        if not job.enabled:
            continue
        sched.add_job(
            execute,
            trigger=trigger_for(job, schedule),
            args=(engine, settings, registry, job),
            id=job.id,
            name=job.id,
            coalesce=True,  # after sleep/downtime, run a missed job once, not N times
            max_instances=1,
            misfire_grace_time=job.misfire_grace_minutes * 60,
            replace_existing=True,
        )
    return sched


def _setup_file_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)  # errors and misfires only


def run_forever(engine: Engine, settings: Settings, registry: dict, schedule: Schedule) -> None:
    problems = validate(schedule, settings, registry)
    if problems:
        raise SystemExit("schedule has problems:\n  " + "\n  ".join(problems))
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(LOCK_PATH))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        raise SystemExit(f"another scheduler is already running (lock: {LOCK_PATH})") from None
    _setup_file_logging()
    try:
        sched = build_scheduler(engine, settings, registry, schedule)
        log.info("scheduler starting with %d jobs; log file %s", len(sched.get_jobs()), LOG_PATH)
        for j in sched.get_jobs():
            log.info("  %-28s next %s", j.id, j.trigger.get_next_fire_time(None, utcnow()))
        sched.start()  # blocks until interrupted
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")
    finally:
        lock.release()


# --- projection ---------------------------------------------------------------


@dataclass
class JobProjection:
    job_id: str
    source: str
    ticks: int = 0
    runs: int = 0
    units: int = 0


def project(
    engine: Engine,
    settings: Settings,
    registry: dict,
    schedule: Schedule,
    start: datetime,
    days: int,
) -> list[JobProjection]:
    """Walk every fire time in [start, start+days), apply gates against the games currently in
    the database, and total the quota units each job would spend. Only as good as the
    schedule data loaded (run nflverse first for NFL)."""
    end = start + timedelta(days=days)
    out = []
    for job in schedule.jobs:
        if not job.enabled:
            continue
        cls = registry[job.source]
        source = cls(engine, settings.source(job.source))
        try:
            cost = source.estimate_cost(job.job, job.params)
        finally:
            source.close()
        p = JobProjection(job.id, job.source)
        for t in fire_times(trigger_for(job, schedule), start, end):
            p.ticks += 1
            if job.when is None or gate_open(engine, job.when, t):
                p.runs += 1
                p.units += cost
        out.append(p)
    return out


def projection_report(
    engine: Engine, settings: Settings, projections: list[JobProjection], days: int
) -> list[str]:
    lines = [f"{'job':30} {'source':14} {'ticks':>6} {'runs':>5} {'units':>6}"]
    for p in projections:
        lines.append(f"{p.job_id:30} {p.source:14} {p.ticks:6} {p.runs:5} {p.units:6}")
    totals: dict[str, int] = {}
    for p in projections:
        totals[p.source] = totals.get(p.source, 0) + p.units
    lines.append("")
    for source, units in totals.items():
        cfg = settings.sources.get(source)
        if cfg is None or cfg.quota is None:
            lines.append(f"{source}: {units} units over {days} days (no quota)")
            continue
        q = QuotaManager(engine, source, cfg.quota).status()
        period_days = (q.period_end - q.period_start).days
        per_period = round(units * period_days / days)
        verdict = "OK" if per_period <= q.usable else "OVER BUDGET"
        lines.append(
            f"{source}: {units} units over {days} days ≈ {per_period} per {cfg.quota.period} "
            f"vs {q.usable} usable ({cfg.quota.limit} limit) -> {verdict}"
        )
    return lines


def next_runs(schedule: Schedule, now: datetime) -> list[tuple[str, datetime | None]]:
    return [
        (j.id, trigger_for(j, schedule).get_next_fire_time(None, now))
        for j in schedule.jobs
        if j.enabled
    ]
