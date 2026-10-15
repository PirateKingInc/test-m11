"""Scheduled job definitions, gating, and execution (no APScheduler here, so it is easy to test)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import Engine, func, select

from betdata.config import PROJECT_ROOT, Settings
from betdata.core.source import RunResult
from betdata.db.schema import games, sports, utcnow

log = logging.getLogger(__name__)
DEFAULT_SCHEDULE = PROJECT_ROOT / "config" / "schedule.yaml"


@dataclass(frozen=True)
class Gate:
    sport: str
    after_minutes: int = 0
    before_minutes: int = 60


@dataclass(frozen=True)
class JobDef:
    id: str
    source: str
    job: str
    cron: str
    params: dict[str, Any] = field(default_factory=dict)
    priority: str = "normal"
    misfire_grace_minutes: int = 30
    enabled: bool = True
    when: Gate | None = None
    active_months: tuple[int, ...] | None = None  # in the schedule's timezone


def in_season(job: JobDef, now: datetime, tz: ZoneInfo) -> bool:
    return job.active_months is None or now.astimezone(tz).month in job.active_months


@dataclass(frozen=True)
class Schedule:
    timezone: ZoneInfo
    jobs: list[JobDef]


def load_schedule(path: Path | None = None) -> Schedule:
    raw = yaml.safe_load((path or DEFAULT_SCHEDULE).read_text()) or {}
    defaults = raw.get("defaults", {})
    jobs, seen = [], set()
    for item in raw.get("jobs", []):
        body = {**defaults, **item}
        if body["id"] in seen:
            raise ValueError(f"duplicate scheduled job id {body['id']!r}")
        seen.add(body["id"])
        when = body.pop("when", None)
        months = body.pop("active_months", None)
        jobs.append(
            JobDef(
                **body,
                when=Gate(**when) if when else None,
                active_months=tuple(months) if months else None,
            )
        )
    return Schedule(ZoneInfo(raw.get("timezone", "UTC")), jobs)


def validate(schedule: Schedule, settings: Settings, registry: dict) -> list[str]:
    """Problems that would make a job fail every time (unknown source/job, bad cron)."""
    from betdata.scheduler.runner import WallClockCronTrigger

    problems = []
    for j in schedule.jobs:
        cls = registry.get(j.source)
        if cls is None:
            problems.append(f"{j.id}: unknown source {j.source!r}")
            continue
        if j.source not in settings.sources:
            problems.append(f"{j.id}: source {j.source!r} missing from sources.yaml")
        elif j.job not in cls.jobs():
            problems.append(f"{j.id}: {j.source} has no job {j.job!r}")
        try:
            WallClockCronTrigger(j.cron, schedule.timezone)
        except ValueError as e:
            problems.append(f"{j.id}: bad cron {j.cron!r}: {e}")
    return problems


def gate_open(engine: Engine, gate: Gate, now: datetime) -> bool:
    lo = now + timedelta(minutes=gate.after_minutes)
    hi = now + timedelta(minutes=gate.before_minutes)
    with engine.connect() as conn:
        n = conn.execute(
            select(func.count())
            .select_from(games.join(sports))
            .where(
                sports.c.key == gate.sport,
                games.c.status != "final",
                games.c.start_time_utc > lo,
                games.c.start_time_utc <= hi,
            )
        ).scalar()
    return n > 0


def execute(
    engine: Engine,
    settings: Settings,
    registry: dict,
    job: JobDef,
    *,
    now: datetime | None = None,
    timezone: ZoneInfo | None = None,
    source_kwargs: dict[str, Any] | None = None,
) -> RunResult | None:
    """Run one scheduled job. Returns None when gated out or not run. Never raises, so one bad
    job cannot take the scheduler down."""
    now = now or utcnow()
    try:
        cfg = settings.source(job.source)
        if not cfg.enabled:
            log.warning("%s: source %s is disabled in sources.yaml; skipping", job.id, job.source)
            return None
        if timezone is not None and not in_season(job, now, timezone):
            log.debug("%s: out of season", job.id)
            return None
        if job.when and not gate_open(engine, job.when, now):
            log.debug("%s: gated out (no %s games in window)", job.id, job.when.sport)
            return None
        source = registry[job.source](engine, cfg, **(source_kwargs or {}))
        try:
            result = source.run(job.job, priority=job.priority, scheduled_job=job.id, **job.params)
        finally:
            source.close()
        log.info(
            "%s: %s inserted=%s seen=%s %s",
            job.id, result.status, result.rows_inserted, result.rows_seen, result.message,
        )  # fmt: skip
        return result
    except Exception:
        log.exception("%s crashed", job.id)
        return None
