"""`betdata nfl props-select` (M15 NFL player props)."""

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
