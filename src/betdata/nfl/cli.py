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


def _data_dir():
    import os
    from pathlib import Path

    from betdata.config import PROJECT_ROOT

    return Path(os.environ.get("BETDATA_DATA_DIR") or PROJECT_ROOT / "data")


@nfl_app.command("derive")
def derive(
    seasons: str = typer.Option(None, help="pbp seasons to rebuild, e.g. 2025 (default: all)"),
):
    """Rebuild derived tables from files already stored: game info from the archived
    schedule, play-by-play aggregates from data/pbp. No network call."""
    from betdata.nfl.derive import derive_game_info, derive_pbp
    from betdata.sources.nflverse import parse_seasons

    years = sorted(parse_seasons(seasons) or []) if seasons else None
    with _engine().begin() as conn:
        n_info = derive_game_info(conn)
        n_pbp = derive_pbp(conn, _data_dir(), years)
    typer.echo(f"game info: {n_info} games; play-by-play: {n_pbp} team/player rows")
