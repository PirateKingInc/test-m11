"""`betdata nfl ...`: M12 data maintenance and checks."""

from __future__ import annotations

import typer

nfl_app = typer.Typer(help="NFL model (M12): as-of data, checks.", no_args_is_help=True)


def _engine():
    from betdata.config import get_settings
    from betdata.db.engine import make_engine

    return make_engine(get_settings().db_url)


@nfl_app.command("backfill-report-dates")
def backfill_report_dates():
    """Fill injury publish times (nflverse date_modified) on rows loaded before they were
    parsed, from the archived raw files. No network call; safe to repeat."""
    from betdata.sources.nflverse_data import fill_report_dates

    with _engine().begin() as conn:
        updated, files = fill_report_dates(conn)
    typer.echo(f"read {files} archived injury files; filled report_date on {updated} rows")
