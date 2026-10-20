"""`betdata nba ...`: NBA history (fetch on Actions, export, load) and, later, the model."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import typer

nba_app = typer.Typer(help="NBA data and model (M14).", no_args_is_help=True)
log = logging.getLogger(__name__)


def _engine():
    from betdata.config import get_settings
    from betdata.db import seed as seed_mod
    from betdata.db.engine import make_engine, upgrade

    settings = get_settings()
    upgrade(settings.db_url)
    engine = make_engine(settings.db_url)
    seed_mod.seed(engine)
    return settings, engine


def _run(settings, engine, source: str, job: str, **params):
    from betdata.sources import REGISTRY

    src = REGISTRY[source](engine, settings.source(source))
    try:
        res = src.run(job, **params)
    finally:
        src.close()
    typer.echo(f"{source} {job} {params}: {res.status} {res.rows_inserted}/{res.rows_seen} "
               f"{res.message}")  # fmt: skip
    return res


@nba_app.command("history-fetch")
def history_fetch(season: int = typer.Option(..., help="Season (start year), e.g. 2023."),
                  odds: bool = True, injuries: bool = True):  # fmt: skip
    """Fetch one season's history that needs Actions: the schedule (sportsdataverse), then
    ESPN's opening/closing lines and the official injury reports, month by month."""
    settings, engine = _engine()
    _run(settings, engine, "sportsdataverse", "schedule", sport="nba", seasons=str(season))
    if odds:
        _run(settings, engine, "espn_nba", "odds_history", season=season, limit=5000)
    if injuries and season >= 2018:
        day, end = date(season, 10, 1), min(date(season + 1, 10, 15), datetime.now(UTC).date())
        if season == 2019:
            end = date(2020, 10, 15)  # the bubble ran to October 2020
        while day <= end:
            last = min(end, (day.replace(day=1) + timedelta(days=32)).replace(day=1)
                       - timedelta(days=1))  # fmt: skip
            try:
                _run(settings, engine, "nba_injury", "history", start=day.isoformat(),
                     end=last.isoformat())  # fmt: skip
            except Exception as e:  # keep the other months; the run is logged as failed
                typer.echo(f"nba_injury history {day}..{last}: crashed: {e!r}")
            day = last + timedelta(days=1)


@nba_app.command("export-history")
def export_history(season: int = typer.Option(...), out: Path = typer.Option(Path("history"))):
    """Write this database's NBA lines and injury reports for one season as gzipped CSV."""
    from betdata.nba.history import export

    _, engine = _engine()
    typer.echo(json.dumps(export(engine, out, season)))


@nba_app.command("load-history")
def load_history(src: Path = typer.Argument(..., help="Directory of exported files.")):
    """Load exported lines and injury reports (no network). Needs the NBA schedule loaded."""
    from betdata.nba.history import load

    _, engine = _engine()
    typer.echo(json.dumps(load(engine, src)))
