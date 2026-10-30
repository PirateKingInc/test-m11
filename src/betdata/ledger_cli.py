"""`betdata ledger ...`: the hourly job's ledger steps (betdata/ledger.py, betdata/weekly.py)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import typer

ledger_app = typer.Typer(help="The permanent pick ledger, weekly report and milestone pushes.")
PENDING = Path("data/reports/ledger_pending.json")


def _p_over():
    """nbap15's calibrated P(over | no push), for tagging props stored before the tag."""
    try:
        from betdata.nba.props.live import PropDesign, load_model, p_over_no_push

        mod, _ = load_model(PropDesign.load())
        return lambda market, mean, line: float(p_over_no_push(mod, market, [mean], [line])[0])
    except Exception:  # noqa: BLE001 - untagged then, never a failed sync
        return None


@ledger_app.command("sync")
def ledger_sync(db: Path = typer.Option(Path("data/poller.sqlite"), help="Poller database.")):
    """Append new picks and newly final results from the poller database to ledger/*.csv."""
    from sqlalchemy import create_engine

    from betdata.config import get_settings
    from betdata.db.engine import make_engine
    from betdata.ledger import ledger_dir, sync

    if not db.is_file():
        typer.echo("ledger: no poller database; nothing to add")
        return
    peng = create_engine(f"sqlite:///file:{db.resolve()}?mode=ro&uri=true", future=True)
    now = datetime.now(UTC)
    with peng.connect() as p, make_engine(get_settings().db_url).connect() as m:
        res = sync(p, m, ledger_dir(), now, _p_over())
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(json.dumps({"milestones": res["milestones"]}))
    typer.echo(f"ledger: {res['picks']} picks and {res['results']} results appended; "
               f"milestones {[m['signal_name'] + ':' + m['graded'] for m in res['milestones']]}")


@ledger_app.command("publish")
def ledger_publish(db: Path = typer.Option(Path("data/poller.sqlite"), help="Poller database.")):
    """The weekly report when due (Mondays after the morning run) and the ntfy pushes: the
    weekly 5-line summary and any milestone (60 / 200 graded picks)."""
    from sqlalchemy import create_engine

    from betdata.config import PROJECT_ROOT, get_settings
    from betdata.db.engine import make_engine
    from betdata.weekly import build, due, milestone_lines, ntfy, report_path

    topic = os.environ.get("NTFY_TOPIC") or None
    now = datetime.now(UTC)
    ms = json.loads(PENDING.read_text()).get("milestones", []) if PENDING.exists() else []
    if ms:
        status = ntfy(topic, "Pick milestone", milestone_lines(ms, PROJECT_ROOT))
        typer.echo(f"milestone push: {status}")
    if not due(PROJECT_ROOT, now):
        typer.echo("weekly report: not due")
        return
    p = (create_engine(f"sqlite:///file:{db.resolve()}?mode=ro&uri=true", future=True).connect()
         if db.is_file() else None)  # fmt: skip
    try:
        with make_engine(get_settings().db_url).connect() as m:
            md, summary = build(PROJECT_ROOT, now, m, p)
    finally:
        if p is not None:
            p.close()
    path = report_path(PROJECT_ROOT, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md)
    typer.echo(f"weekly report: wrote {path.relative_to(PROJECT_ROOT)}; push "
               f"{ntfy(topic, 'Weekly picks report', summary)}")
