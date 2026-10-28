"""`betdata runner ...`: the hourly run (GitHub Actions calls `run`, then `failed-page` if it
failed) and `betdata page build` for a local preview."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import typer

runner_app = typer.Typer(help="Hourly runner and page (M13).", no_args_is_help=True)
page_app = typer.Typer(help="The static page.", no_args_is_help=True)

SITE = Path("site")


def _now(at: str | None) -> datetime:
    from betdata.db.schema import utcnow

    return datetime.fromisoformat(at) if at else utcnow()


@runner_app.command("run")
def run_cmd(
    site: Path = typer.Option(SITE, help="Where to write the page."),
    last_good: Path = typer.Option(None, help="Copy of the last good page (default: "
                                             "<data dir>/site_last_good)."),  # fmt: skip
    trigger: str = typer.Option("local", help="schedule, clock, workflow_dispatch or local."),
    fail: str = typer.Option(None, help="Force a failure with this message (failure test)."),
    at: str = typer.Option(None, help="Pretend it is this UTC time (ISO); for testing."),
):
    """One hourly run: rebuild lost state, refresh data due this hour, predict, grade, publish."""
    from betdata.config import get_settings
    from betdata.db import seed as seed_mod
    from betdata.db.engine import make_engine, upgrade
    from betdata.nfl.workflow import data_dir
    from betdata.page.build import build_site
    from betdata.runner.hourly import AUTOMATIC_TRIGGERS, run_hourly, slot_done
    from betdata.runner.window import slot
    from betdata.sources import REGISTRY
    from betdata.sports import active_sports, load_entries

    settings = get_settings()
    upgrade(settings.db_url)  # new or rebuilt database: create the schema
    engine = make_engine(settings.db_url)
    seed_mod.seed(engine)
    entries = load_entries()
    active = active_sports(entries)
    now = _now(at)
    if trigger in AUTOMATIC_TRIGGERS and slot_done(engine, now):
        typer.echo(f"slot already done: a scheduled run for {slot(now):%Y-%m-%d %H:00} UTC "
                   "succeeded; nothing to do")  # fmt: skip
        return
    try:
        report = run_hourly(engine, settings, REGISTRY, active, now, trigger=trigger, fail=fail)
    except Exception as e:
        typer.echo(f"run failed: {type(e).__name__}: {e}", err=True)
        raise typer.Exit(1) from None
    with engine.connect() as conn:
        status = build_site(conn, settings, entries, active, site,
                            max(report.as_of or now, _now(at)))  # fmt: skip
    good = last_good or data_dir() / "site_last_good"
    if good.exists():
        shutil.rmtree(good)
    shutil.copytree(site, good)
    typer.echo(json.dumps({"run_id": report.run_id, "status": report.status,
                           "rebuilt": report.rebuilt, "seconds": report.seconds,
                           "sports": report.sports, "jobs": report.jobs,
                           "missing_keys": status["missing_keys"],
                           "engine_sections": status.get("engine_sections")}, indent=1,
                          default=str))  # fmt: skip


@runner_app.command("ensure-state")
def ensure_state_cmd(trigger: str = typer.Option("ensure-state")):
    """Make sure the database exists and has history (for workflows other than the hourly one,
    such as the weather backfill): if it is empty, do one full run, which rebuilds it."""
    from betdata.config import get_settings
    from betdata.db import seed as seed_mod
    from betdata.db.engine import make_engine, upgrade
    from betdata.runner.hourly import has_history, run_hourly
    from betdata.sources import REGISTRY
    from betdata.sports import active_sports

    settings = get_settings()
    upgrade(settings.db_url)
    engine = make_engine(settings.db_url)
    seed_mod.seed(engine)
    active = active_sports()
    if all(has_history(engine, s.key) for s in active):
        typer.echo("state present")
        return
    report = run_hourly(engine, settings, REGISTRY, active, _now(None), trigger=trigger)
    typer.echo(f"state rebuilt (run {report.run_id}, {report.seconds:.0f} s)")


@runner_app.command("failed-page")
def failed_page_cmd(
    message: str = typer.Option(..., help="What failed."),
    site: Path = typer.Option(SITE),
    last_good: Path = typer.Option(None),
    url: str = typer.Option(None, help="Link to the failed workflow run."),
):
    """Put the last good page back in `site` with the failure warning switched on."""
    from betdata.nfl.workflow import data_dir
    from betdata.page.build import mark_failed

    mark_failed(site, last_good or data_dir() / "site_last_good", _now(None), message, url)
    typer.echo(f"wrote failure page to {site}")


@page_app.command("build")
def page_build_cmd(site: Path = typer.Option(SITE), at: str = typer.Option(None)):
    """Build the page from the database without running anything (local preview)."""
    from betdata.config import get_settings
    from betdata.db.engine import make_engine
    from betdata.page.build import build_site
    from betdata.sports import active_sports, load_entries

    settings = get_settings()
    entries = load_entries()
    with make_engine(settings.db_url).connect() as conn:
        build_site(conn, settings, entries, active_sports(entries), site, _now(at))
    typer.echo(f"wrote {site / 'index.html'}")
