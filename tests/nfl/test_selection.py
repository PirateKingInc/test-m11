"""Model selection, calibration and the final test's scoring rules."""

import pytest

from betdata.analysis.dataset import GameRow
from betdata.analysis.models import Prediction
from betdata.nfl.final import bets_at_close, final_test, tally
from betdata.nfl.groups import GROUPS
from betdata.nfl.pipeline import build, outcomes
from betdata.nfl.points import FINAL_REPORT, PREKICK
from betdata.nfl.selection import Design, calibration, fit_design, gbm_mae, predict_rows
from tests.nfl.synth import make_store

DESIGN = Design(["base", "avail", "str"], ["base", "avail", "env"], 100.0, 30.0)


@pytest.fixture(scope="module")
def setup():
    store = make_store()
    from betdata.nfl.features.availability import learn_play_prob

    store = store.with_params(**learn_play_prob(store, [2018]))
    rows = build(store, [GROUPS[g] for g in DESIGN.groups], list(store.games.index))
    return store, rows, outcomes(store)


def test_design_round_trip(tmp_path):
    p = tmp_path / "d.json"
    DESIGN.save(p)
    assert Design.load(p) == DESIGN
    assert DESIGN.groups == ["base", "avail", "str", "env"]


def test_fit_predict_and_calibration(setup):
    store, rows, out = setup
    fits = fit_design(rows, out, DESIGN, [2018, 2019])
    pred = predict_rows(rows[rows.season == 2020], fits)
    assert set(pred.point) == {FINAL_REPORT, PREKICK}
    assert pred.win_prob.between(0, 1).all()
    c = calibration(pred[pred.point == PREKICK], out)
    assert 0 < c["brier"] < 0.5 and sum(b[2] for b in c["bins"]) == c["n"]


def test_gbm_challenger_runs(setup):
    _, rows, out = setup
    res = gbm_mae(rows, out, DESIGN, [2018, 2019], [2020])
    assert set(res) == {FINAL_REPORT, PREKICK} and all(v[0] > 0 for v in res.values())


@pytest.mark.parametrize(
    "pm, pt, spread, total, hs, as_, want",
    [
        (10.0, None, -3.0, None, 30, 20, {"spread": "win", "total": None}),
        (0.0, None, -3.0, None, 30, 20, {"spread": "loss", "total": None}),
        (1.0, None, -3.0, None, 23, 20, {"spread": None, "total": None}),  # edge 2 < 3
        (None, 52.0, None, 47.0, 24, 23, {"spread": None, "total": "push"}),
        (None, 40.0, None, 47.0, 10, 13, {"spread": None, "total": "win"}),
    ],
)  # fmt: skip
def test_bets_at_close(pm, pt, spread, total, hs, as_, want):
    assert bets_at_close(pm, pt, spread, total, hs, as_) == want


def test_tally_ci_and_roi():
    b = tally("x", ["win", "loss", "win", None, "push"]).as_dict()
    assert b["record"] == "2-1-1" and b["bets"] == 4
    assert b["ci95"][0] < 2 / 3 < b["ci95"][1]


def test_final_test_is_scored_once(setup, tmp_path):
    store, rows, out = setup
    g = store.games
    games = [GameRow(int(i), int(g.at[i, "season"]), "REG", int(g.at[i, "week"]),
                     g.at[i, "kickoff"], int(g.at[i, "home"]), int(g.at[i, "away"]), "H", "A",
                     False, 20 + int(out.at[i, "margin"]), 20, -3.0, -110, -110, 44.0, -110,
                     -110, 7.0, 7.0)
             for i in g.index if i in out.index]  # fmt: skip
    units = {gr.game_id: Prediction(1.0, total=44.0) for gr in games}
    path = tmp_path / "final.json"
    rep = final_test(rows, out, DESIGN, games, units, path, fit_seasons=(2018, 2019),
                     test_seasons=(2020,))  # fmt: skip
    pt = rep["points"][PREKICK]
    assert pt["games"] > 0 and set(pt["margin_mae"]) == {"m12", "elo", "m11", "close"}
    assert path.exists()
    with pytest.raises(RuntimeError, match="scored once"):
        final_test(rows, out, DESIGN, games, units, path, fit_seasons=(2018, 2019),
                   test_seasons=(2020,))  # fmt: skip
