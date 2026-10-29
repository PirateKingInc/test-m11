"""`betdata poller ...` and `betdata alerts ...` (M16)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

poller_app = typer.Typer(help="M16 injury-news and price poller (clock workflow).",
                         no_args_is_help=True)  # fmt: skip
alerts_app = typer.Typer(help="M16 alerts: lag study, grading, page section.",
                         no_args_is_help=True)  # fmt: skip


def _until(minutes: int | None, now: datetime) -> datetime:
    if minutes is not None:
        return now + timedelta(minutes=minutes)
    # Before the next clock run starts (it cancels this one at about :00:30): stop at :56.
    end = now.replace(minute=56, second=0, microsecond=0)
    return end if end > now else end + timedelta(hours=1)


@poller_app.command("run")
def poller_run(
    minutes: int = typer.Option(None, help="Run this many minutes (default: until :56)."),
    db: Path = typer.Option(Path("data/poller.sqlite"), help="The poller's own database."),
):
    """Poll until the end of this clock hour. Reads the hourly state as a throwaway copy
    (BETDATA_DB_URL); writes only the poller database."""
    from betdata.alerts.db import engine_for
    from betdata.alerts.poller import Keys, Poller
    from betdata.config import get_settings
    from betdata.db.engine import make_engine

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    now = datetime.now(UTC)
    main = make_engine(get_settings().db_url)
    try:
        from betdata.alerts.nfl import NflHandler

        nfl = NflHandler()
    except ImportError:
        nfl = None
    p = Poller(main, engine_for(db), Keys.from_env(), nfl_handler=nfl)
    cycles = p.run(_until(minutes, now))
    typer.echo(f"poller: {cycles} cycles, {p.skipped} skipped (hourly run), requests "
               f"{dict(p.http.count)}, errors {len(p.errors)}")  # fmt: skip
    for e in p.errors[-10:]:
        typer.echo(f"  {e}")


@alerts_app.command("lag")
def alerts_lag(
    db: Path = typer.Option(Path("data/poller.sqlite"), help="The poller's database."),
    decide: bool = typer.Option(False, help="Take the Signal 1 decision if two weeks are up."),
    events: bool = typer.Option(False, help="List every event's lag per book."),
):
    """The lag study so far (Part E): lag per book, DK+FD pooled median, Signal 1 status."""
    from betdata.alerts import lag as L
    from betdata.alerts.db import engine_for
    from betdata.config import get_settings
    from betdata.db.engine import make_engine

    now = datetime.now(UTC)
    main = make_engine(get_settings().db_url)
    with engine_for(db).begin() as p, main.connect() as m:
        lags = L.all_lags(p, m, now)
        decision = L.decide(p, m, now) if decide else L.get_state(p, "signal1:decision")
        for line in L.report_lines(lags, decision, L.study_start(p)):
            typer.echo(line)
        if events:
            for x in lags:
                lag = "-" if x.minutes is None else f"{x.minutes:.1f}{'+' if x.censored else ''}"
                typer.echo(f"  {x.published:%m-%d %H:%M} {x.sport} game {x.game_id} "
                           f"{x.player} ({x.change}) {x.book}: {lag} min {x.market or ''}")


@alerts_app.command("replay-value")
def alerts_replay_value(
        db: Path = typer.Option(Path("data/poller.sqlite"), help="Poller database."),
        out: Path = typer.Option(Path("data/reports/m18_replay.json"))):  # fmt: skip
    """M18: replay the value rule on every stored NBA news event (a throwaway copy of the
    poller database; nothing is pushed) and grade the replayed alerts vs the consensus close."""
    import json

    from betdata.alerts.replay import replay
    from betdata.config import get_settings
    from betdata.db.engine import make_engine

    res = replay(make_engine(get_settings().db_url), db, out.with_suffix(".sqlite"),
                 datetime.now(UTC))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1, default=str) + "\n")
    typer.echo(json.dumps(res, indent=1, default=str)[:12000])


@alerts_app.command("quota")
def alerts_quota(days: int = typer.Option(30, help="Days ahead, on the stored schedule.")):
    """30-day request projection of the poller, with the hourly job's share of shared sources."""
    from betdata.alerts.quota import project
    from betdata.config import get_settings
    from betdata.db.engine import make_engine
    from betdata.runner.hourly import runner_schedule
    from betdata.scheduler.runner import project as hourly_project
    from betdata.sources import REGISTRY
    from betdata.sports import active_sports

    settings = get_settings()
    engine = make_engine(settings.db_url)
    start = datetime.now(UTC)
    hourly = hourly_project(engine, settings, REGISTRY, runner_schedule(active_sports()), start,
                            days)  # fmt: skip
    odds = _units(hourly, "the_odds_api")
    with engine.connect() as c:
        for line in project(c, start, days).lines(odds):
            typer.echo(line)


def _units(projection, source: str) -> int:
    """The hourly projection's units for one source (a list of JobProjection rows)."""
    return int(sum(p.units for p in projection if p.source == source))


def grade_alerts(db: Path) -> None:
    """Grade alerts against the pre-game close and print the report (shared with `betdata grade
    alerts`). Reads the main database; writes only the poller database."""
    from betdata.alerts.db import engine_for
    from betdata.alerts.grade import grade_all, report_lines
    from betdata.config import get_settings
    from betdata.db.engine import make_engine

    pdb = engine_for(db)
    n = grade_all(make_engine(get_settings().db_url), pdb, datetime.now(UTC))
    typer.echo(f"alert grades written: {n}")
    with pdb.connect() as p:
        for line in report_lines(p):
            typer.echo(line)


@alerts_app.command("grade")
def alerts_grade(db: Path = typer.Option(Path("data/poller.sqlite"), help="Poller database.")):
    """Grade every alert whose game has started (idempotent) and report CLV, ROI and the
    success-rule verdict per signal."""
    grade_alerts(db)


@alerts_app.command("page")
def alerts_page(db: Path = typer.Option(Path("data/poller.sqlite"), help="Poller database."),
                site: Path = typer.Option(Path("site"), help="The built site.")):  # fmt: skip
    """Add the Alerts section to the built site/index.html and write site/alerts.json. Never
    fails: without a readable poller database the section says "Alerts unavailable"."""
    from betdata.alerts.page import publish

    typer.echo(publish(db, site))
