"""One hourly run, for every active sport: the data jobs due this hour, predictions, grading,
and the page. Shared by all sports; nothing here names one.

  1. If the database has no history (the saved state was lost), rebuild it from free sources
     (each sport's `bootstrap`) and remember that, so the page can say so.
  2. Each sport's jobs whose cron matches this hour (Eastern) and whose gate is open. A job
     failing, or skipped because its API key is not set, does not stop the run; it is recorded
     and shown on the page.
  3. Each sport predicts (no refitting) and grades.
  4. The run is recorded in runner_runs; the caller builds the page from the database.

A crash in steps 1 or 3 marks the run failed (and re-raises): the workflow then republishes the
last good page with a failure warning.
"""

from __future__ import annotations

import logging
import time as clock
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, insert, select, update

from betdata.config import Settings
from betdata.db.schema import games, runner_runs, sports, utcnow
from betdata.runner.window import EASTERN, slot
from betdata.scheduler.jobs import JobDef, gate_open, in_season
from betdata.sports import Sport

log = logging.getLogger(__name__)
MIN_HISTORY_GAMES = 500  # fewer final games than this for an active sport = state lost
AUTOMATIC_TRIGGERS = ("schedule", "clock")  # GitHub's cron, or the clock workflow
LIVE_SLACK = timedelta(minutes=30)  # a run whose `now` is this close to the wall clock is live


@dataclass
class RunReport:
    run_id: int
    slot_at: datetime
    status: str = "running"
    rebuilt: bool = False
    jobs: list[dict[str, Any]] = field(default_factory=list)
    sports: dict[str, dict[str, Any]] = field(default_factory=dict)
    seconds: float = 0.0
    message: str = ""
    as_of: datetime | None = None  # the instant predictions, grades and the page are as of


def runner_schedule(active: list[Sport] | None = None):
    """Every active sport's jobs as one schedule (Eastern), for `scheduler plan --runner`."""
    from betdata.scheduler.jobs import Schedule
    from betdata.sports import active_sports

    return Schedule(EASTERN, [j for s in (active or active_sports()) for j in s.jobs])


def due(job: JobDef, slot_at: datetime) -> bool:
    """Whether a job's cron (Eastern, on the hour) fires at this hourly slot."""
    from betdata.scheduler.runner import WallClockCronTrigger

    trig = WallClockCronTrigger(job.cron, EASTERN)
    nxt = trig.get_next_fire_time(None, slot_at - timedelta(seconds=1))
    return nxt is not None and nxt == slot_at.astimezone(nxt.tzinfo)


def slot_done(engine: Engine, now: datetime) -> bool:
    """Whether a scheduled run for this hourly slot already succeeded (the backup trigger then
    has nothing to do)."""
    with engine.connect() as conn:
        n = conn.execute(
            select(func.count()).select_from(runner_runs)
            .where(runner_runs.c.slot_at == slot(now),
                   runner_runs.c.trigger.in_(AUTOMATIC_TRIGGERS),
                   runner_runs.c.status == "ok")
        ).scalar()  # fmt: skip
    return n > 0


def has_history(engine: Engine, sport: str) -> bool:
    with engine.connect() as conn:
        n = conn.execute(
            select(func.count()).select_from(games.join(sports))
            .where(sports.c.key == sport, games.c.status == "final")
        ).scalar()  # fmt: skip
    return n >= MIN_HISTORY_GAMES


def run_job(engine: Engine, settings: Settings, registry: dict, job: JobDef, now: datetime,
            source_kwargs: dict | None) -> dict[str, Any]:  # fmt: skip
    out: dict[str, Any] = {"id": job.id, "source": job.source, "job": job.job}
    cfg = settings.source(job.source)
    if not job.enabled or not cfg.enabled:
        return {**out, "status": "disabled"}
    if not in_season(job, now, EASTERN):
        return {**out, "status": "out of season"}
    if job.when and not gate_open(engine, job.when, now):
        return {**out, "status": "no games in window"}
    if cfg.api_key_env and not cfg.api_key:
        return {**out, "status": "skipped", "message": f"{cfg.api_key_env} not set"}
    t = clock.monotonic()
    source = registry[job.source](engine, cfg, **(source_kwargs or {}))
    try:
        res = source.run(job.job, priority=job.priority, scheduled_job=job.id, **job.params)
    finally:
        source.close()
    return {**out, "status": res.status, "message": res.message, "rows": res.rows_inserted,
            "seconds": round(clock.monotonic() - t, 1)}  # fmt: skip


def import_sharp_props(engine: Engine, now: datetime) -> dict[str, Any]:
    """M18 Part E: DK/FD props from the poller's archive into `prop_lines` (no ESPN props)."""
    from betdata.nfl.book_lines import poller_conn
    from betdata.props.sharp_import import import_props

    out: dict[str, Any] = {"sport": "all", "id": "sharp-props", "source": "sharpapi",
                           "job": "props from the poller archive"}  # fmt: skip
    t = clock.monotonic()
    poller = poller_conn()
    try:
        with engine.begin() as conn:
            wall = utcnow()
            n = import_props(conn, poller, max(now, wall) if wall - now < LIVE_SLACK else now)
        out.update(status="ok" if poller is not None else "skipped", rows=n,
                   message=None if poller is not None else "no poller database")
    except Exception as e:  # noqa: BLE001 - the page keeps the last stored lines
        log.exception("sharp props import failed")
        out.update(status="failed", message=f"{type(e).__name__}: {e}"[:300])
    finally:
        if poller is not None:
            poller.close()
    out["seconds"] = round(clock.monotonic() - t, 1)
    return out


def run_hourly(engine: Engine, settings: Settings, registry: dict, active: list[Sport],
               now: datetime, *, trigger: str = "local", source_kwargs: dict | None = None,
               fail: str | None = None) -> RunReport:  # fmt: skip
    """`fail` forces a failure after the run starts (the failure test)."""
    t0 = clock.monotonic()
    slot_at = slot(now)
    with engine.begin() as conn:
        run_id = conn.execute(insert(runner_runs).values(
            slot_at=slot_at, started_at=now, trigger=trigger, status="running", rebuilt=False,
        )).inserted_primary_key[0]  # fmt: skip
    report = RunReport(run_id, slot_at)
    try:
        if fail:
            raise RuntimeError(fail)
        for sport in active:
            if not has_history(engine, sport.key):
                log.warning("%s: no history in the database; rebuilding from free sources",
                            sport.key)  # fmt: skip
                sport.bootstrap(engine, now, source_kwargs)
                report.rebuilt = True
        for sport in active:
            for job in sport.jobs:
                if due(job, slot_at):
                    report.jobs.append({"sport": sport.key, **run_job(
                        engine, settings, registry, job, now, source_kwargs)})  # fmt: skip
        report.jobs.append(import_sharp_props(engine, now))
        # Data fetched during this run is stamped with the time it arrived, after `now`;
        # predict, grade and publish as of the end of the refresh so it is all used. A run for
        # a past instant (`--at`, tests) moves on by the elapsed time only, not to today.
        wall = utcnow()
        live = wall - now < LIVE_SLACK
        report.as_of = as_of = (max(now, wall) if live
                                else now + timedelta(seconds=clock.monotonic() - t0))  # fmt: skip
        for sport in active:
            st: dict[str, Any] = {}
            t = clock.monotonic()
            st["predicted"] = sport.predict(engine, as_of)
            st["predict_seconds"] = round(clock.monotonic() - t, 1)
            st.update(sport.run_details())
            with engine.begin() as conn:
                st["graded"] = sport.grade(conn, as_of)
            report.sports[sport.key] = st
        report.status = "ok"
    except Exception as e:
        report.status, report.message = "failed", f"{type(e).__name__}: {e}"
        raise
    finally:
        report.seconds = round(clock.monotonic() - t0, 1)
        with engine.begin() as conn:
            conn.execute(update(runner_runs).where(runner_runs.c.run_id == run_id).values(
                status=report.status,
                finished_at=max(report.as_of or now, now + timedelta(seconds=report.seconds)),
                rebuilt=report.rebuilt, message=report.message or None,
                details={"jobs": report.jobs, "sports": report.sports,
                         "seconds": report.seconds},
            ))  # fmt: skip
    return report
