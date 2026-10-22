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
