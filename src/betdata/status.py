"""`betdata status`: one screen answering "is ingestion healthy and within budget?"."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Engine, case, desc, func, select

from betdata.config import Settings
from betdata.core.quota import QuotaManager
from betdata.db.schema import (
    games,
    ingest_runs,
    odds_snapshots,
    source_health,
    sports,
    unmatched_entities,
)
from betdata.scheduler.jobs import Schedule
from betdata.scheduler.runner import fire_times, trigger_for


@dataclass
class StatusReport:
    lines: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _fmt(t: datetime | None, tz) -> str:
    return t.astimezone(tz).strftime("%a %m-%d %H:%M") if t else "never"


def _expected_interval(job, schedule: Schedule, now: datetime) -> timedelta | None:
    """Longest gap between consecutive fire times over the next week (None if < 2 fires)."""
    times = list(fire_times(trigger_for(job, schedule), now, now + timedelta(days=8)))
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    return max(gaps) if gaps else None


def build_status(
    engine: Engine, settings: Settings, schedule: Schedule, now: datetime
) -> StatusReport:
    r = StatusReport()
    tz = schedule.timezone
    with engine.connect() as conn:
        health = {h.source: h for h in conn.execute(select(source_health))}

        def last_run(job_id, status=None):
            q = select(ingest_runs).where(ingest_runs.c.scheduled_job == job_id)
            if status:
                q = q.where(ingest_runs.c.status == status)
            return conn.execute(q.order_by(desc(ingest_runs.c.run_id)).limit(1)).first()

        r.lines.append(f"status at {_fmt(now, tz)} ({tz.key})")
        r.lines.append("")
        r.lines.append("SCHEDULED JOBS")
        for job in schedule.jobs:
            if not job.enabled:
                continue
            last = last_run(job.id)
            last_ok = last_run(job.id, "ok")
            nxt = trigger_for(job, schedule).get_next_fire_time(None, now)
            last_txt = f"{last.status} {_fmt(last.started_at, tz)}" if last else "never run"
            gated = (
                f" (when {job.when.sport} games ≤{job.when.before_minutes}m)" if job.when else ""
            )
            r.lines.append(f"  {job.id:28} last {last_txt:22} next {_fmt(nxt, tz)}{gated}")
            if job.when is None:
                interval = _expected_interval(job, schedule, now)
                grace = timedelta(minutes=job.misfire_grace_minutes)
                if interval and (
                    last_ok is None or now - last_ok.started_at > 2 * interval + grace
                ):
                    since = _fmt(last_ok.started_at if last_ok else None, tz)
                    r.problems.append(
                        f"{job.id}: no successful run since {since} (expected every {interval})"
                    )

        r.lines.append("")
        r.lines.append("SOURCES")
        for name, cfg in settings.sources.items():
            if not cfg.enabled:
                continue
            h = health.get(name)
            if h is None:
                r.lines.append(f"  {name:14} no calls yet")
                continue
            state = "OK"
            if h.open_until and h.open_until > now:
                state = f"PAUSED until {_fmt(h.open_until, tz)}"
                r.problems.append(
                    f"{name}: circuit open after {h.consecutive_failures} failures: {h.last_error}"
                )
            elif h.consecutive_failures:
                state = f"{h.consecutive_failures} recent failure(s)"
            r.lines.append(
                f"  {name:14} {state:24} last success {_fmt(h.last_success, tz)}"
                f"  last failure {_fmt(h.last_failure, tz)}"
            )

        recent_failures = conn.execute(
            select(ingest_runs)
            .where(
                ingest_runs.c.status == "failed", ingest_runs.c.started_at > now - timedelta(days=1)
            )
            .order_by(desc(ingest_runs.c.run_id))
        ).all()
        for f in recent_failures:
            r.problems.append(
                f"run {f.run_id} {f.source}.{f.job} failed {_fmt(f.started_at, tz)}: {f.message}"
            )

        r.lines.append("")
        r.lines.append("QUOTA")
        for name, cfg in settings.sources.items():
            if not cfg.enabled or not cfg.quota:
                continue
            s = QuotaManager(engine, name, cfg.quota, clock=lambda: now).status()
            r.lines.append(
                f"  {name:14} {s.used_effective}/{s.limit} used, {s.remaining_usable} usable left "
                f"until {_fmt(s.period_end, tz)}; today {s.used_today}/{s.daily_allowance}"
            )
            if s.remaining_usable == 0:
                r.problems.append(f"{name}: usable quota exhausted until {_fmt(s.period_end, tz)}")

        r.lines.append("")
        r.lines.append("DATA")
        for key, n_games, upcoming in conn.execute(
            select(
                sports.c.key,
                func.count(games.c.game_id),
                func.sum(case((games.c.start_time_utc > now, 1), else_=0)),
            )
            .select_from(sports.outerjoin(games))
            .group_by(sports.c.key)
            .order_by(sports.c.key)
        ):
            r.lines.append(f"  {key:6} {n_games:6} games ({upcoming or 0} upcoming)")
        for source, n, latest in conn.execute(
            select(
                odds_snapshots.c.source, func.count(), func.max(odds_snapshots.c.fetched_at)
            ).group_by(odds_snapshots.c.source)
        ):
            latest_txt = _fmt(latest, tz) if isinstance(latest, datetime) else str(latest)[:16]
            r.lines.append(f"  odds from {source:14} {n:8} snapshots, latest {latest_txt}")
        unmatched = conn.execute(
            select(func.count()).where(unmatched_entities.c.resolved.is_(False))
        ).scalar()
        r.lines.append(
            f"  unmatched names: {unmatched}" + (" (see `betdata unmatched`)" if unmatched else "")
        )
        if unmatched:
            r.problems.append(f"{unmatched} unmatched team/book names (`betdata unmatched`)")

    r.lines.append("")
    if r.problems:
        r.lines.append("PROBLEMS")
        r.lines += [f"  - {p}" for p in r.problems]
    else:
        r.lines.append("no problems")
    return r
