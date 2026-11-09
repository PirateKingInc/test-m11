"""`betdata diag rules-v2`: the rules v2 diagnostics report (reporting only)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

diag_app = typer.Typer(help="Diagnostics (reporting only).", no_args_is_help=True)
OUT = Path("reports/nba")


@diag_app.command("rules-v2")
def rules_v2(db: bool = typer.Option(False, help="Also the database half (a runner)."),
             out: Path = typer.Option(OUT)):  # fmt: skip
    from betdata import diagnostics as D
    from betdata.ledger import ledger_dir

    led = D.ledger_part(ledger_dir())
    dbp = None
    db_json = out / "rules_v2_diagnostics_db.json"
    if db:
        from betdata.config import get_settings
        from betdata.db.engine import make_engine

        with make_engine(get_settings().db_url).connect() as c:
            dbp = D.db_part(c)
        db_json.write_text(json.dumps(dbp, indent=1, default=str) + "\n")
    elif db_json.exists():
        dbp = json.loads(db_json.read_text())
    out.mkdir(parents=True, exist_ok=True)
    (out / "rules_v2_diagnostics_ledger.json").write_text(
        json.dumps(led, indent=1, default=str) + "\n")
    (out / "rules_v2_diagnostics.md").write_text(D.report(led, dbp))
    typer.echo(str(out / "rules_v2_diagnostics.md"))
