"""`betdata morning run` and `betdata morning push` (the 6:15 AM ET update, betdata/morning.py;
.github/workflows/hourly.yml with trigger `morning`)."""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import typer

morning_app = typer.Typer(help="The 6:15 AM ET morning update.", no_args_is_help=True)
SITE = Path("site")
POLLER_DB = Path("data/poller.sqlite")


@morning_app.command("run")
def run_cmd(site: Path = typer.Option(SITE), db: Path = typer.Option(POLLER_DB),
            at: str = typer.Option(None, help="Pretend it is this UTC time (ISO).")):  # fmt: skip
    """Grade, ledger, engine, select both cards, build the page (a. to e.)."""
    from sqlalchemy import insert, update

    from betdata import morning as M
    from betdata.alerts.db import engine_for
    from betdata.alerts.poller import Keys, Poller
    from betdata.config import get_settings
    from betdata.db import seed as seed_mod
    from betdata.db.engine import make_engine, upgrade
    from betdata.db.schema import runner_runs
    from betdata.nfl.workflow import data_dir
    from betdata.page.build import build_site
    from betdata.runner.window import slot
    from betdata.sources import REGISTRY
    from betdata.sports import active_sports, load_entries

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    upgrade(settings.db_url)
    main = make_engine(settings.db_url)
    seed_mod.seed(main)
    entries = load_entries()
    active = active_sports(entries)
    now = datetime.fromisoformat(at) if at else datetime.now(UTC)
    if not M.sports_due(main, now):
        typer.echo("morning: no NBA or NFL games yesterday or today; nothing to do")
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write("skipped=true\n")
        return
    try:
        from betdata.alerts.nfl import NflHandler

        nfl = NflHandler()
    except ImportError:
        nfl = None
    pdb = engine_for(db)
    poller = Poller(main, pdb, Keys.from_env(), nfl_handler=nfl)
    with main.begin() as c:
        run_id = c.execute(insert(runner_runs).values(
            slot_at=slot(now), started_at=now, trigger="morning", status="running",
            rebuilt=False)).inserted_primary_key[0]  # fmt: skip

    def build():
        # the page reads "Last updated" from the latest successful run: mark this one done
        # before building (a failed publish sets it back to failed below), or the page shows
        # the previous run's time and a false "data is hours old" warning
        t = datetime.now(UTC) if not at else now
        with main.begin() as c:
            c.execute(update(runner_runs).where(runner_runs.c.run_id == run_id).values(
                status="ok", finished_at=t))  # fmt: skip
        with main.connect() as conn:
            build_site(conn, settings, entries, active, site, t)

    rep = M.run(main, pdb, poller, now, settings=settings, registry=REGISTRY, active=active,
                build=build)  # fmt: skip
    status = "ok" if "publish" in rep.steps and not any(
        e.startswith("publish") for e in rep.errors) else "failed"  # fmt: skip
    with main.begin() as c:
        c.execute(update(runner_runs).where(runner_runs.c.run_id == run_id).values(
            status=status, finished_at=datetime.now(UTC) if not at else now,
            message="; ".join(rep.errors)[:500] or None,
            details={"morning": {"steps": rep.steps, "cards": rep.cards,
                                 "shadow": rep.shadow, "refresh": rep.refresh,
                                 "engine_picks": rep.engine_picks}}))  # fmt: skip
    good = data_dir() / "site_last_good"
    if status == "ok" and site.exists():
        if good.exists():
            shutil.rmtree(good)
        shutil.copytree(site, good)
    typer.echo(json.dumps(rep.__dict__, indent=1, default=str))
    if status != "ok":
        raise typer.Exit(1)


@morning_app.command("push")
def push_cmd(db: Path = typer.Option(POLLER_DB)):
    """f. ONE ntfy message per sport (after the page is deployed)."""
    from betdata import morning as M
    from betdata.alerts.db import engine_for
    from betdata.ledger import ledger_dir

    sent = M.push(engine_for(db), ledger_dir(), datetime.now(UTC), os.environ.get("NTFY_TOPIC"))
    typer.echo(f"morning push: {sent}")


__all__ = ["morning_app"]
