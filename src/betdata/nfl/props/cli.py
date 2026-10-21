"""`betdata nfl props-select | props-final-test | props-freeze` (M15 NFL player props)."""

from __future__ import annotations

import typer


def _line(mk: str, r: dict) -> str:
    m, b = r["model"], r["baseline"]
    extra = (f"  Brier {r['brier']:.4f} (season TD rate {r['brier_baseline']:.4f})"
             if "brier_baseline" in r else "")  # fmt: skip
    return (f"{mk:11s} n={r['n']:6d}  MAE {m['mae']:7.3f} (baseline {b['mae']:7.3f})  "
            f"RMSE {m['rmse']:7.3f} ({b['rmse']:7.3f}){extra}")


def register(app: typer.Typer) -> None:
    @app.command("props-select")
    def props_select_cmd(rebuild: bool = typer.Option(False, help="Rebuild the features.")):
        """M15 NFL props on validation (2020-2022): error vs the last-10 average (anytime TD:
        season TD rate), calibration at a synthetic line, the adjustments, the yards family and
        Platt decisions. Writes reports/nfl/props_selection.json."""
        from betdata.nfl.props.select import select

        res = select(rebuild)
        for mk, r in res["validation"].items():
            typer.echo(_line(mk, r) + f"  [{res['decisions']['variant'][mk]}]")
        typer.echo(f"yards families: {res['decisions']['families']}; Platt: "
                   f"{sorted(res['decisions']['platt'])}")  # fmt: skip

    @app.command("props-final-test")
    def props_final_test_cmd(again: bool = typer.Option(False, help="Score the test again.")):
        """Score the test seasons (2023-2025) once with the validated design refitted on
        2013-2022; compare with stored prop lines where there are any. Writes
        reports/nfl/props_final_test.json. No betting backtest (no lines in validation)."""
        from betdata.nfl.cli import _engine
        from betdata.nfl.props.final import final_test

        with _engine().connect() as conn:
            try:
                res = final_test(conn, again)
            except RuntimeError as e:
                raise typer.BadParameter(str(e)) from e
        for mk, r in res["markets"].items():
            typer.echo(_line(mk, r))
        typer.echo(f"vs lines: {res['vs_lines']}")

    @app.command("props-freeze")
    def props_freeze_cmd():
        """Fit the validated design on 2013-2025: config/fits/nfl_props_2013_2025.json and
        config/nfl_props.json (every market research-only)."""
        from betdata.nfl.props.final import freeze

        typer.echo(freeze())
