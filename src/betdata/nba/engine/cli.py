"""`betdata nba engine ...`: every M20 step, reproducible from the database (MODEL.md "M20").

  stability     the B2 table on training seasons (reports/nba/m20_stability.csv)
  select        B3-B6 choices on validation (reports/nba/m20_selection.json)
  fit           training constants for a season range (--last 2020 | 2022 | 2025)
  simulate      engine probabilities at stored lines for seasons (a pickle, for design/test)
  design        Part C/E on validation sims (blend weights, calibration, theta)
  final-test    the test seasons, scored once (refuses a second run)
  freeze        the live fit config/fits/nba_engine_m20.json (constants 2015-2025 + design)
  live          write the live document now (the hourly run does this)
"""

from __future__ import annotations

import json
import pickle
from datetime import UTC, datetime
from pathlib import Path

import typer

engine_app = typer.Typer(help="M20 NBA engine.", no_args_is_help=True)
REPORTS = Path("reports/nba")
TRAIN_LAST, VALID, TEST = 2020, (2021, 2022), (2023, 2024, 2025)


def _engine():
    from betdata.nba.cli import _engine as e

    return e()[1]


def _inputs(conn, sigma: float = 16.33):
    from betdata.nba.data import load_injuries
    from betdata.nba.engine import data as E
    from betdata.nba.engine import fitting as F

    g, p, tg = E.load(conn, 2015, sigma)
    return F.Inputs(g, p, tg, load_injuries(conn, g))


def _play() -> dict:
    return json.loads(Path("config/fits/nba_fit_2015_2025.json").read_text())["play_prob"]


@engine_app.command("stability")
def stability_cmd():
    from betdata.nba.engine.stability import stability

    with _engine().connect() as c:
        inp = _inputs(c)
    stability(inp.p, inp.tg, range(2015, TRAIN_LAST + 1)).to_csv(
        REPORTS / "m20_stability.csv", index=False)  # fmt: skip
    typer.echo("reports/nba/m20_stability.csv")


@engine_app.command("select")
def select_cmd():
    from betdata.nba.engine import select as S
    from betdata.nba.engine.stability import paddings, stability

    with _engine().connect() as c:
        inp = _inputs(c)
    pads = paddings(stability(inp.p, inp.tg, range(2015, TRAIN_LAST + 1)))
    sel = S.Selector(inp, pads, _play())
    sel.run()
    sel.write(REPORTS / "m20_selection.json")


@engine_app.command("fit")
def fit_cmd(last: int = typer.Option(TRAIN_LAST), out: Path = typer.Option(...)):
    """Constants (paddings, play by minutes, multipliers, simulation) on 2015..last."""
    from betdata.nba.engine import fitting as F
    from betdata.nba.engine.stability import paddings, stability
    from betdata.nba.engine.states import Params

    with _engine().connect() as c:
        inp = _inputs(c)
    seasons = range(2015, last + 1)
    pads = paddings(stability(inp.p, inp.tg, seasons))
    prm = Params(**json.loads((REPORTS / "m20_selection.json").read_text())["params"])
    fit = F.base_fit(inp, pads, prm, _play(), seasons)
    eng = F.Engine(inp, fit, addons=[])
    F.fit_play_by_minutes(eng, seasons)
    F.fit_multipliers(eng, seasons)
    F.fit_sim(eng, seasons, n_games=250, n_sims=2000)
    fit.sim["script"], fit.sim["addons"] = False, []  # D1-D4 did not clear the bar
    out.write_text(json.dumps(fit.to_json(), indent=1, default=float))


@engine_app.command("simulate")
def simulate_cmd(fit: Path = typer.Option(...), seasons: str = typer.Option(...),
                 out: Path = typer.Option(...), n: int = 10_000):  # fmt: skip
    from betdata.nba.engine import evaluate as V
    from betdata.nba.engine import fitting as F
    from betdata.nba.engine.project import Fit
    from betdata.nba.props.select import history_lines

    with _engine().connect() as c:
        inp = _inputs(c)
        side = V.side_lines(c, inp.g)
        pl = history_lines(c)
    pl = pl.rename(columns={"open_line": "line", "open_over_price": "over_price",
                            "open_under_price": "under_price"}).dropna(subset=["line"])
    eng = F.Engine(inp, Fit.from_json(json.loads(fit.read_text())))
    ss = [int(x) for x in seasons.split(",")]
    gids = inp.g[inp.g.season.isin(ss) & (inp.g.status == "final")].game_id
    games, props, d = V.simulate(eng, gids, pl[pl.game_id.isin(set(gids))], side, n)
    out.write_bytes(pickle.dumps({"games": games, "props": props, "means": d,
                                  "means_morning": eng.predict(gids, ("morning",)),
                                  "side": side, "plines": pl}))  # fmt: skip


@engine_app.command("freeze")
def freeze_cmd(fit: Path = typer.Option(..., help="Constants on 2015-2025 (`fit --last 2025`)."),
               design: Path = typer.Option(REPORTS / "m20_design.json")):  # fmt: skip
    """The live config: constants fitted on every completed season with the frozen design,
    plus the validation blend/calibration/threshold (never refitted hourly)."""
    from betdata.nba.engine import VERSION
    from betdata.nba.engine.live import FIT_PATH

    d = json.loads(design.read_text())
    raw = {"version": VERSION, "fit": json.loads(fit.read_text()),
           "design": {k: d[k] for k in ("w", "platt", "theta", "validated")}}
    FIT_PATH.write_text(json.dumps(raw, indent=1, default=float) + "\n")
    typer.echo(str(FIT_PATH))


@engine_app.command("live")
def live_cmd():
    from betdata.nba.engine.live import run

    typer.echo(f"{run(_engine(), datetime.now(UTC))} games")


__all__ = ["engine_app"]
