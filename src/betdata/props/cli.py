"""`betdata props ...`: ESPN player prop line history (fetch on Actions, export, load)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from betdata.nba.cli import _engine, _run

props_app = typer.Typer(help="Player prop lines (M15).", no_args_is_help=True)
SCHEDULE = {"nba": ("sportsdataverse", "schedule", {"sport": "nba"}),
            "nfl": ("nflverse", "schedules", {})}  # fmt: skip


@props_app.command("history-fetch")
def history_fetch(sport: str = typer.Option(..., help="nba or nfl"),
                  season: int = typer.Option(..., help="Season (start year), e.g. 2024.")):
    """Load the season's schedule, then fetch every finished game's opening and closing prop
    lines from ESPN (at most two books per game, one request per second)."""
    settings, engine = _engine()
    source, job, params = SCHEDULE[sport]
    _run(settings, engine, source, job, seasons=str(season), **params)
    _run(settings, engine, "espn_props", "history", sport=sport, season=season)


@props_app.command("export-history")
def export_history(sport: str = typer.Option(...), season: int = typer.Option(...),
                   out: Path = typer.Option(Path("history"))):  # fmt: skip
    """Write this database's prop line history for one sport and season as gzipped CSV."""
    from betdata.props.history import export

    _, engine = _engine()
    typer.echo(json.dumps({"rows": export(engine, out, sport, season)}))


@props_app.command("load-history")
def load_history(src: Path = typer.Argument(..., help="Directory of exported files.")):
    """Load exported prop line history (no network). Needs the schedules loaded."""
    from betdata.props.history import load

    _, engine = _engine()
    typer.echo(json.dumps(load(engine, src)))
