"""Verification (d): a simulated 30-day month of hourly runs on a full NFL schedule stays inside
every source's free limit after its safety margin. Walks every hourly slot the workflow fires,
runs the runner's own `due` and gate logic for every NFL job, and adds up worst-case costs."""

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import insert, select

from betdata.core.matching import sport_id
from betdata.db.schema import games, teams
from betdata.runner.hourly import due, runner_schedule
from betdata.runner.window import EASTERN, cron_fires, in_window
from betdata.scheduler.jobs import gate_open, in_season
from betdata.scheduler.runner import project
from betdata.sources import REGISTRY
from betdata.sports import active_sports

START = date(2026, 10, 1)  # a Thursday
# A busy NFL week in Eastern time: (weekday, hour, minute, games). Sunday has a London game.
WEEK = [(3, 20, 15, 1), (6, 9, 30, 1), (6, 13, 0, 9), (6, 16, 5, 2), (6, 16, 25, 2),
        (6, 20, 20, 1), (0, 19, 15, 1), (0, 20, 15, 1)]  # fmt: skip
# Worst-case units per run: SharpAPI paginates (3 pages); Open-Meteo forecasts every outdoor
# game of the next 7 days (16 locations x 2 two-week blocks); The Odds API 3 markets x 1 region.
WORST = {"sharpapi": 3, "open_meteo": 32, "the_odds_api": 3, "oddspapi": 1}


def _schedule(conn) -> list[datetime]:
    sid = sport_id(conn, "nfl")
    team_ids = conn.execute(select(teams.c.team_id).where(teams.c.sport_id == sid)).scalars().all()
    kicks = []
    for day in range(35):
        d = START + timedelta(days=day)
        for wd, h, m, n in WEEK:
            if d.weekday() == wd:
                t = datetime.combine(d, time(h, m), tzinfo=EASTERN).astimezone(UTC)
                kicks += [t] * n
    conn.execute(insert(games), [
        dict(sport_id=sid, season=2026, week=1 + i // 18, start_time_utc=t,
             home_team_id=team_ids[(2 * i) % 32], away_team_id=team_ids[(2 * i + 1) % 32],
             status="scheduled")
        for i, t in enumerate(kicks)])  # fmt: skip
    return kicks


def test_a_month_of_hourly_runs_stays_inside_every_quota(engine, settings):
    with engine.begin() as conn:
        kicks = _schedule(conn)
    [nfl] = active_sports()
    start = datetime.combine(START, time(0), tzinfo=UTC)
    slots = [t for d in range(30) for t in cron_fires(START + timedelta(days=d))
             if in_window(t) and t >= start]  # fmt: skip
    assert len(slots) == 30 * 16
    spend: dict[tuple[str, str], int] = defaultdict(int)  # (source, day or month) -> units
    odds_runs = 0
    for slot_at in slots:
        for job in nfl.jobs:
            if not (due(job, slot_at) and in_season(job, slot_at, EASTERN)):
                continue
            if job.when and not gate_open(engine, job.when, slot_at):
                continue
            cost = WORST.get(job.source, 0) if job.job in ("odds", "forecast") else 0
            day = slot_at.astimezone(EASTERN).date().isoformat()
            spend[(job.source, day)] += cost
            spend[(job.source, "month")] += cost
            odds_runs += job.id == "nfl-odds-prekick"
    # The Odds API: one snapshot per kickoff window, never hourly.
    windows = {t for t in kicks if start <= t < start + timedelta(days=30)}
    assert odds_runs <= len(windows)
    for name, cfg in settings.sources.items():
        if cfg.quota is None:
            continue
        usable = int(cfg.quota.limit * (1 - cfg.quota.safety_margin))
        if cfg.quota.period == "day":
            worst_day = max((u for (s, k), u in spend.items() if s == name and k != "month"),
                            default=0)  # fmt: skip
            assert worst_day <= usable, (name, worst_day, usable)
        else:
            assert spend[(name, "month")] <= usable, (name, spend[(name, "month")], usable)
    assert spend[("the_odds_api", "month")] <= 3 * len(windows) < 450
    assert spend[("sharpapi", "month")] == 3 * 30 * 16  # every run, inside 3,000 a day

    # `betdata scheduler plan --runner` agrees: nothing over budget.
    from betdata.scheduler.runner import projection_report

    report = projection_report(engine, settings, project(
        engine, settings, REGISTRY, runner_schedule([nfl]), start, 30), 30)  # fmt: skip
    assert not [line for line in report if "OVER BUDGET" in line]
