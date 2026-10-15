"""Scheduler: DST-safe triggers, gating, crash isolation, projection, status, and a simulated
unattended week."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from filelock import FileLock
from sqlalchemy import func, select

from betdata.config import load_settings
from betdata.db.schema import ingest_runs, source_health
from betdata.scheduler import runner
from betdata.scheduler.jobs import (
    Gate,
    JobDef,
    Schedule,
    execute,
    gate_open,
    load_schedule,
    validate,
)
from betdata.scheduler.runner import WallClockCronTrigger, fire_times, project
from betdata.sources import REGISTRY
from betdata.status import build_status
from tests.conftest import fixture_bytes

NY = ZoneInfo("America/New_York")
SAMPLE = fixture_bytes("nflverse", "games_sample.csv")


class Router:
    """Fake upstreams for every source, routed by URL path."""

    def __init__(self):
        self.calls: list[str] = []
        self.fail_odds = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        host = request.url.host
        if host == "api.sharpapi.io":
            return httpx.Response(
                200, json={"data": [], "meta": {"pagination": {"has_more": False}}}
            )
        if host == "api.oddspapi.io":
            return httpx.Response(200, json=[])
        if "college" in host:
            sport = "football" if "football" in host else "basketball"
            return httpx.Response(
                200, content=fixture_bytes("cfbd", f"{path.strip('/')}_{sport}.json")
            )
        if "/sports/" in path and "americanfootball_nfl" not in path:
            return httpx.Response(200, content=b"[]", headers={"x-requests-last": "0"})
        if path.endswith("/schedules/games.csv"):
            return httpx.Response(200, content=SAMPLE)
        if path.endswith("/events"):
            body = fixture_bytes("the_odds_api", "nfl_events.json")
            return httpx.Response(200, content=body, headers={"x-requests-last": "0"})
        if path.endswith("/odds"):
            if self.fail_odds:
                return httpx.Response(500, content=b"upstream down")
            body = fixture_bytes("the_odds_api", "nfl_odds_snapshot1.json")
            return httpx.Response(200, content=body, headers={"x-requests-last": "3"})
        return httpx.Response(404)


@pytest.fixture
def router():
    return Router()


@pytest.fixture
def kwargs(router, clock):
    return dict(transport=httpx.MockTransport(router), sleep=lambda s: None, clock=clock)


@pytest.fixture
def schedule():
    return load_schedule()


def runs(engine, *where):
    with engine.connect() as conn:
        return conn.execute(select(ingest_runs).where(*where).order_by(ingest_runs.c.run_id)).all()


def load_sample_schedule(engine, settings, kwargs):
    src = REGISTRY["nflverse"](engine, settings.source("nflverse"), **kwargs)
    assert src.run("schedules").status == "ok"


# --- config and triggers -------------------------------------------------------


def test_shipped_schedule_is_valid(schedule, settings):
    assert validate(schedule, settings, REGISTRY) == []
    assert {j.id for j in schedule.jobs} >= {"odds-nfl-prekick", "nflverse-schedules-morning"}


def test_validate_catches_mistakes(settings):
    bad = Schedule(
        NY,
        [
            JobDef("a", "nope", "x", "0 * * * *"),
            JobDef("b", "nflverse", "missing_job", "0 * * * *"),
            JobDef("c", "nflverse", "schedules", "99 * * * *"),
        ],
    )
    problems = validate(bad, settings, REGISTRY)
    assert len(problems) == 3


def fmt(times):
    return [f"{t:%m-%d %H:%M%Z}" for t in times]


def test_trigger_dst_fall_back_fires_each_wall_time_once():
    t = WallClockCronTrigger("50 * * * *", NY)
    start = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)  # 00:00 EDT, the night clocks go back
    got = fmt(fire_times(t, start, start + timedelta(hours=5)))
    assert got == ["11-01 00:50EDT", "11-01 01:50EDT", "11-01 02:50EST", "11-01 03:50EST"]


def test_trigger_dst_spring_forward_shifts_missing_time():
    t = WallClockCronTrigger("50 * * * *", NY)
    start = datetime(2027, 3, 14, 5, 0, tzinfo=UTC)  # 00:00 EST
    got = fmt(fire_times(t, start, start + timedelta(hours=5)))
    assert got == ["03-14 00:50EST", "03-14 01:50EST", "03-14 03:50EDT", "03-14 04:50EDT",
                   "03-14 05:50EDT"]  # fmt: skip


def test_daily_trigger_keeps_local_time_across_dst():
    t = WallClockCronTrigger("15 7 * * *", NY)
    got = list(fire_times(t, datetime(2026, 10, 31, tzinfo=UTC), datetime(2026, 11, 3, tzinfo=UTC)))
    assert [x.strftime("%H:%M") for x in got] == ["07:15", "07:15", "07:15"]
    assert [x.astimezone(UTC).hour for x in got] == [11, 12, 12]


# --- gating and execution -------------------------------------------------------


def test_gate_window(engine, settings, kwargs):
    load_sample_schedule(engine, settings, kwargs)
    kick = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)  # CHI @ ATL
    gate = Gate("nfl", 0, 60)
    assert gate_open(engine, gate, kick - timedelta(minutes=10))
    assert gate_open(engine, gate, kick - timedelta(minutes=60))
    assert not gate_open(engine, gate, kick - timedelta(minutes=61))
    assert not gate_open(engine, gate, kick)  # already kicked off
    assert not gate_open(engine, Gate("nba", 0, 60), kick - timedelta(minutes=10))


def test_gated_out_job_makes_no_call_and_no_run(engine, settings, kwargs, router, clock):
    job = JobDef("pre", "the_odds_api", "odds", "50 * * * *", {"sport": "nfl"},
                 when=Gate("nfl", 0, 60))  # fmt: skip
    assert execute(engine, settings, REGISTRY, job, now=clock(), source_kwargs=kwargs) is None
    assert router.calls == [] and runs(engine) == []


def test_crashing_job_does_not_raise(engine, settings, kwargs, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug in a source")

    monkeypatch.setattr(REGISTRY["nflverse"], "run", boom)
    job = JobDef("x", "nflverse", "schedules", "0 * * * *")
    assert execute(engine, settings, REGISTRY, job, source_kwargs=kwargs) is None


def test_disabled_source_is_skipped(engine, kwargs, router, monkeypatch, tmp_path):
    cfg = tmp_path / "sources.yaml"
    cfg.write_text("sources:\n  nflverse:\n    enabled: false\n")
    settings = load_settings(config_path=cfg, env_path=tmp_path / "none")
    job = JobDef("x", "nflverse", "schedules", "0 * * * *")
    assert execute(engine, settings, REGISTRY, job, source_kwargs=kwargs) is None
    assert router.calls == []


def test_scheduled_runs_are_tagged(engine, settings, kwargs):
    job = JobDef("nflverse-test", "nflverse", "schedules", "0 * * * *")
    result = execute(engine, settings, REGISTRY, job, source_kwargs=kwargs)
    assert result.status == "ok"
    assert runs(engine)[0].scheduled_job == "nflverse-test"


def test_build_scheduler_registers_enabled_jobs(engine, settings, schedule):
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = runner.build_scheduler(engine, settings, REGISTRY, schedule, BackgroundScheduler)
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs) == {j.id for j in schedule.jobs if j.enabled}
    assert all(isinstance(j.trigger, WallClockCronTrigger) for j in jobs.values())
    assert jobs["odds-nfl-prekick"].misfire_grace_time == 15 * 60


def test_second_scheduler_refuses_to_start(engine, settings, schedule, tmp_path, monkeypatch):
    lock_path = tmp_path / "scheduler.lock"
    monkeypatch.setattr(runner, "LOCK_PATH", lock_path)
    with FileLock(str(lock_path)), pytest.raises(SystemExit, match="already running"):
        runner.run_forever(engine, settings, REGISTRY, schedule)


# --- projection -----------------------------------------------------------------


def test_projection_counts_only_gated_in_ticks(engine, settings, kwargs, schedule):
    load_sample_schedule(engine, settings, kwargs)
    start = datetime(2026, 10, 15, 4, 0, tzinfo=UTC)  # Thu 00:00 ET
    by_id = {p.job_id: p for p in project(engine, settings, REGISTRY, schedule, start, 4)}
    pre = by_id["odds-nfl-prekick"]
    # SEA@DEN Thu 20:15 ET, HOU@JAX Sun 09:30 ET (London), CHI@ATL Sun 13:00 ET
    assert (pre.ticks, pre.runs, pre.units) == (96, 3, 9)
    assert by_id["nflverse-schedules-morning"].units == 0


# --- a simulated unattended week -------------------------------------------------


def test_simulated_week_unattended(engine, settings, kwargs, schedule, router, clock):
    cfbd = REGISTRY["cfbd"](engine, settings.source("cfbd"), **kwargs)
    assert cfbd.run("teams_football").status == "ok"  # one-time setup before going unattended
    start = datetime(2026, 10, 15, 4, 0, tzinfo=UTC)  # Thu 00:00 ET
    end = start + timedelta(days=7)
    ticks = sorted(
        (
            (t, job)
            for job in schedule.jobs
            for t in fire_times(runner.trigger_for(job, schedule), start, end)
        ),
        key=lambda tj: (tj[0], tj[1].id),
    )
    for t, job in ticks:
        clock.now = t
        execute(engine, settings, REGISTRY, job, now=t, timezone=schedule.timezone,
                source_kwargs=kwargs)  # fmt: skip

    all_runs = runs(engine)
    # Allowed non-ok: OddsPapi leagues not configured yet are skipped without a call.
    bad = [
        (r.scheduled_job, r.status, r.message)
        for r in all_runs
        if r.status != "ok" and not (r.message or "").startswith("NotConfigured")
    ]
    assert bad == []
    by_job = {}
    for r in all_runs:
        by_job[r.scheduled_job] = by_job.get(r.scheduled_job, 0) + 1
    assert by_job["nflverse-schedules-morning"] == 7
    assert by_job["nflverse-schedules-night"] == 7
    # Thu 06:30 is before the first schedule load (no games known yet), and the sample has no
    # week-7 games, so after Sunday nothing is within 10 days: only Fri, Sat, Sun run.
    assert by_job["odds-nfl-events"] == 3
    # Thu SEA@DEN, Sun HOU@JAX (London), Sun 1pm slate, Sun 4pm slate (KC@LV from odds data)
    assert by_job["odds-nfl-prekick"] == 4
    assert router.calls.count("/v4/sports/americanfootball_nfl/odds") == 4
    # college football in season: daily games + lines, plus Saturday-night scores
    assert by_job["cfbd-ncaaf-games"] == 7 and by_job["cfbd-ncaaf-lines"] == 7
    assert by_job["cfbd-ncaaf-games-saturday-night"] == 1
    assert "cfbd-ncaab-games" not in by_job  # basketball starts in November
    # SharpAPI: every 10 min in the 3 h before each kickoff window; nothing for idle sports
    assert by_job["sharp-nfl-near"] > 0 and "sharp-nba-near" not in by_job
    # OddsPapi: Sunday before the early games + Thursday night (sample has no Monday game);
    # skipped without a request because no NFL market map is configured yet
    assert by_job["papi-nfl"] == 1 and by_job["papi-nfl-primetime"] == 1
    assert not [c for c in router.calls if "odds-by-tournaments" in c]

    q = runner.QuotaManager(engine, "the_odds_api", settings.source("the_odds_api").quota,
                            clock=clock).status()  # fmt: skip
    assert q.used_effective == 12

    clock.now = end
    report = build_status(engine, settings, schedule, end)
    assert report.problems == [], report.problems


def test_status_reports_failures_and_paused_sources(
    engine, settings, kwargs, schedule, router, clock
):
    router.fail_odds = True
    load_sample_schedule(engine, settings, kwargs)
    job = next(j for j in schedule.jobs if j.id == "odds-nfl-prekick")
    kick = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)
    for i in range(5):  # five failing pre-kick ticks trips the breaker
        clock.now = kick - timedelta(minutes=10 + i)
        execute(engine, settings, REGISTRY, job, now=clock.now, source_kwargs=kwargs)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(source_health)).scalar() >= 1
    report = build_status(engine, settings, schedule, clock.now)
    text = "\n".join(report.problems)
    assert "circuit open" in text and "failed" in text


def test_out_of_season_job_does_not_run(engine, settings, kwargs, router, schedule):
    job = next(j for j in schedule.jobs if j.id == "odds-ncaab-events")  # Nov-Apr
    october = datetime(2026, 10, 15, 10, 40, tzinfo=UTC)
    december = datetime(2026, 12, 15, 11, 40, tzinfo=UTC)
    run = execute(engine, settings, REGISTRY, job, now=october, timezone=NY, source_kwargs=kwargs)
    assert run is None and router.calls == []
    run = execute(engine, settings, REGISTRY, job, now=december, timezone=NY, source_kwargs=kwargs)
    assert run.status == "ok"
    assert router.calls == ["/v4/sports/basketball_ncaab/events"]


@pytest.mark.parametrize(
    "dow,expected",
    [
        ("6", ["Sat"]),
        ("0", ["Sun"]),
        ("7", ["Sun"]),
        ("0,6", ["Sat", "Sun"]),
        ("1-5", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
        ("sat", ["Sat"]),
    ],
)
def test_cron_weekdays_use_standard_numbering(dow, expected):
    t = WallClockCronTrigger(f"40 11 * * {dow}", NY)
    start = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)  # Mon 00:00 ET
    days = [x.strftime("%a") for x in fire_times(t, start, start + timedelta(days=7))]
    assert sorted(days) == sorted(expected)
