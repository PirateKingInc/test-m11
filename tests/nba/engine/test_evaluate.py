"""M20 Parts C-E helpers: blend weight, one Platt fit, pick threshold; add-ons switchable."""

import numpy as np

from betdata.nba.engine import evaluate as V
from betdata.nba.engine import fitting as F
from tests.nba.engine.conftest import PLAY
from tests.nba.engine.league import NO_INJ, frames


def test_weight_prefers_the_informative_source():
    rng = np.random.default_rng(0)
    truth = rng.uniform(0.3, 0.7, 4000)
    y = (rng.random(4000) < truth).astype(float)
    w, _ = V.choose_weight(truth, np.full(4000, 0.5), y)
    assert w >= 0.8
    w2, _ = V.choose_weight(np.full(4000, 0.5), truth, y)
    assert w2 <= 0.2


def test_platt_applied_only_when_miscalibrated():
    rng = np.random.default_rng(1)
    p = rng.uniform(0.2, 0.8, 20000)
    y = (rng.random(20000) < p).astype(float)
    assert V.calibrate(p, y)[0] is None  # calibrated: no fit applied
    over = np.clip(0.5 + 2 * (p - 0.5), 0.01, 0.99)  # overconfident
    cal, info = V.calibrate(over, y)
    assert cal is not None and info["slope"] < 0.7


def test_theta_needs_enough_picks():
    ev = np.linspace(0, 0.2, 1000)
    won = np.ones(1000, bool)
    th, ok, rows = V.choose_theta(ev, won, np.zeros(1000, bool), np.full(1000, -110))
    assert ok and all(r["picks"] >= 0 for r in rows) and th in V.THETAS


def test_addons_switch_on_and_keep_team_sums(pads):
    g, p, tg, _ = frames()
    inp = F.Inputs(g, p, tg, NO_INJ)
    fit = F.base_fit(inp, pads, F.Params(), PLAY, [2021])
    fit.sim["d2_beta"], fit.sim["d2_lg_share3"] = {"G": 0.1, "F": 0.0, "C": -0.3}, 0.4
    base = F.Engine(inp, fit, addons=[]).predict(g.game_id[-3:])
    on = F.Engine(inp, fit, addons=["d1", "d2", "d3"]).predict(g.game_id[-3:])
    for d in (base, on):
        t = d.groupby(["game_id", "team_id"]).apply(lambda r: (r.p_play * r.ast_m).sum())
        assert (t > 0).all()
    assert len(base) == len(on)
