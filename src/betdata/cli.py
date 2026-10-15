from __future__ import annotations

import logging

import typer
from sqlalchemy import desc, select

from betdata.config import get_settings
from betdata.core.quota import QuotaManager
from betdata.db import seed as seed_mod
from betdata.db.engine import make_engine, upgrade
from betdata.db.schema import ingest_runs, teams, unmatched_entities, utcnow
from betdata.sources import REGISTRY

app = typer.Typer(help="Sports data ingestion.", no_args_is_help=True)
db_app = typer.Typer(help="Database setup.", no_args_is_help=True)
app.add_typer(db_app, name="db")
sched_app = typer.Typer(help="Unattended scheduled polling.", no_args_is_help=True)
app.add_typer(sched_app, name="scheduler")


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v")):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _engine():
    return make_engine(get_settings().db_url)


def _source(name: str):
    name = name.replace("-", "_")
    if name not in REGISTRY:
        raise typer.BadParameter(f"unknown source {name!r}; available: {sorted(REGISTRY)}")
    return REGISTRY[name](_engine(), get_settings().source(name))


@db_app.command("init")
def db_init():
    """Create/upgrade the schema and load seed data."""
    settings = get_settings()
    upgrade(settings.db_url)
    counts = seed_mod.seed(make_engine(settings.db_url))
    typer.echo(f"database ready at {settings.db_url}; seeded {counts}")


@db_app.command("seed")
def db_seed():
    """(Re)load seeds/*.csv. Safe to repeat."""
    typer.echo(seed_mod.seed(_engine()))


@app.command()
def run(
    source: str,
    job: str,
    sport: str = typer.Option(None, help="nfl, ncaaf, nba, ncaab (odds sources)"),
    seasons: str = typer.Option(None, help="2026 | 2015-2026 | all (nflverse; default current)"),
    priority: str = typer.Option(None, help="normal (paced) or high (skips pacing, never the cap)"),
):
    """Run one job, e.g. `betdata run the-odds-api odds --sport nfl`,
    `betdata run nflverse schedules --seasons all` (backfill)."""
    params = {k: v for k, v in dict(sport=sport, seasons=seasons).items() if v is not None}
    result = _source(source).run(job, priority=priority, **params)
    typer.echo(
        f"run {result.run_id}: {result.status} inserted={result.rows_inserted} "
        f"seen={result.rows_seen} {result.message}"
    )
    if result.status == "failed":
        raise typer.Exit(1)


@app.command()
def quota(source: str = typer.Argument(None)):
    """Show budget status for metered sources."""
    settings, engine = get_settings(), _engine()
    for name, cfg in settings.sources.items():
        if not cfg.quota or (source and name != source.replace("-", "_")):
            continue
        s = QuotaManager(engine, name, cfg.quota).status()
        reported = "n/a" if s.remaining_reported is None else s.remaining_reported
        typer.echo(
            f"{name:14} used {s.used_effective}/{s.limit} (usable {s.usable}) "
            f"remaining_usable={s.remaining_usable} api_reported_remaining={reported} "
            f"today={s.used_today}/{s.daily_allowance} period_end={s.period_end:%Y-%m-%d}"
        )


@app.command()
def runs(limit: int = 20):
    """Recent ingestion runs."""
    with _engine().connect() as conn:
        for r in conn.execute(
            select(ingest_runs).order_by(desc(ingest_runs.c.run_id)).limit(limit)
        ):
            typer.echo(
                f"{r.run_id:5} {r.started_at:%Y-%m-%d %H:%M} {r.source}.{r.job} {r.status} "
                f"ins={r.rows_inserted} seen={r.rows_seen} {r.message or ''}"
            )


@app.command()
def reparse(source: str, raw_id: int):
    """Re-run parse+load on a stored raw response (no API call, no quota)."""
    inserted, seen = _source(source).reparse(raw_id)
    typer.echo(f"raw {raw_id}: inserted={inserted} seen={seen}")


@app.command()
def unmatched():
    """Names we could not map to a team. Add them to seeds/team_aliases.csv, then `db seed`."""
    with _engine().connect() as conn:
        rows = conn.execute(
            select(unmatched_entities, teams.c.canonical_name.label("suggestion"))
            .outerjoin(teams, teams.c.team_id == unmatched_entities.c.suggested_team_id)
            .where(unmatched_entities.c.resolved.is_(False))
            .order_by(desc(unmatched_entities.c.times_seen))
        )
        for r in rows:
            typer.echo(
                f"{r.source:14} {r.kind:5} {r.raw_value!r} x{r.times_seen} suggest={r.suggestion}"
            )


@app.command()
def status(
    check: bool = typer.Option(False, help="Exit 1 if there are problems (for monitoring)."),
):
    """Health, last/next runs, quota, data counts and problems."""
    from betdata.scheduler.jobs import load_schedule
    from betdata.status import build_status

    report = build_status(_engine(), get_settings(), load_schedule(), utcnow())
    typer.echo("\n".join(report.lines))
    if check and report.problems:
        raise typer.Exit(1)


@sched_app.command("run")
def scheduler_run():
    """Run the scheduler in the foreground (use systemd/launchd/tmux to keep it alive)."""
    from betdata.scheduler.jobs import load_schedule
    from betdata.scheduler.runner import run_forever

    run_forever(_engine(), get_settings(), REGISTRY, load_schedule())


@sched_app.command("list")
def scheduler_list():
    """Validate config/schedule.yaml and show each job's next fire time."""
    from betdata.scheduler.jobs import load_schedule, validate
    from betdata.scheduler.runner import next_runs

    schedule = load_schedule()
    for problem in validate(schedule, get_settings(), REGISTRY):
        typer.echo(f"PROBLEM {problem}")
    for job_id, nxt in next_runs(schedule, utcnow()):
        when = nxt.astimezone(schedule.timezone).strftime("%a %Y-%m-%d %H:%M %Z") if nxt else "-"
        typer.echo(f"{job_id:30} next {when}")


@sched_app.command("plan")
def scheduler_plan(days: int = typer.Option(30, help="How many days ahead to project.")):
    """Project quota spend from the schedule and the games already in the database."""
    from betdata.scheduler.jobs import load_schedule
    from betdata.scheduler.runner import project, projection_report

    settings, engine = get_settings(), _engine()
    projections = project(engine, settings, REGISTRY, load_schedule(), utcnow(), days)
    typer.echo("\n".join(projection_report(engine, settings, projections, days)))


if __name__ == "__main__":
    app()
