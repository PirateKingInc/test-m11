"""`betdata nfl engine ...` (M17)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer

engine_app = typer.Typer(help="M17 market-free NFL engine.", no_args_is_help=True)


def _client(source: str, headers: dict | None = None):
    """A polite client (one request a second) logging to the configured database."""
    from betdata.config import get_settings
    from betdata.core.http import SourceClient
    from betdata.db.engine import make_engine
    from betdata.nfl.engine.web import USER_AGENT

    engine = make_engine(get_settings().db_url)
    return SourceClient(engine, source, "", min_interval=1.0, user_agent=USER_AGENT,
                        headers=headers, max_attempts=3)  # fmt: skip


@engine_app.command("fetch")
def fetch(what: str = typer.Argument(..., help="coordinators | lines | timing | fantasy"),
          season: int = typer.Option(None, help="lines: one season"),
          first: int = typer.Option(2016), last: int = typer.Option(2026),
          out: Path = typer.Option(Path("m17data"))):  # fmt: skip
    """Actions-only fetches for Parts A, D and F (network, one request a second)."""
    from betdata.nfl.engine import web

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if what == "coordinators":
        with _client("wikipedia") as c:
            res = web.fetch_coordinators(c, range(first, last + 1), out)
    elif what == "lines":
        with _client("espn_nfl_odds") as c:
            res = web.fetch_lines(c, season, out)
    elif what == "timing":
        with _client("github_api", {"Accept": "application/vnd.github+json"}) as c:
            res = web.fetch_timing(c, out)
    elif what == "fantasy":
        res = web.fetch_fantasy(lambda name, h: _client(f"probe_{name}", h), out)
    else:
        raise typer.BadParameter(what)
    typer.echo(json.dumps(res, default=str)[:4000])


@engine_app.command("stability")
def stability(out: Path = typer.Option(Path("reports/nfl/stability.csv"))):
    """A1: split-half and season-to-season reliability and padding of every engine rate."""
    from betdata.nfl.engine.stability import write

    t = write(out)
    typer.echo(f"{len(t)} rows -> {out}")


@engine_app.command("checks")
def checks(out: Path = typer.Option(Path("reports/nfl/m17_checks.json"))):
    """A2 route proxy and A3 pressure flags."""
    from betdata.nfl.engine.checks import write

    typer.echo(json.dumps(write(out), indent=1))


@engine_app.command("freeze")
def freeze_cmd(first: int = typer.Option(2016), last: int = typer.Option(2025),
               rates: str = typer.Option("{}", help="RateParams as JSON (the validated set)")):
    """Fit the drive model on first..last, calibrate on the last two, write the frozen fit."""
    from betdata.nfl.engine.live import freeze

    seasons = range(first, last + 1)
    path = freeze(seasons, (last - 1, last), json.loads(rates))
    typer.echo(f"frozen fit -> {path}")


@engine_app.command("live")
def live_cmd(at: str = typer.Option(None, help="Run as of this UTC instant (replays only)")):
    """One live engine run now (what the hourly NFL plugin does): predict and store."""
    from datetime import UTC, datetime

    from betdata.config import get_settings
    from betdata.db.engine import make_engine
    from betdata.nfl.engine.live import run

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    now = datetime.fromisoformat(at).replace(tzinfo=UTC) if at else datetime.now(UTC)
    typer.echo(json.dumps(run(make_engine(get_settings().db_url), now), default=str))
