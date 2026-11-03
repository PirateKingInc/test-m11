"""M20 B8: sum constraints hold in every simulated game; outputs are distributions."""

import numpy as np

from betdata.nba.engine import fitting as F
from betdata.nba.engine import sim as SM
from tests.nba.engine.conftest import PLAY
from tests.nba.engine.league import NO_INJ, frames


def _sims(pads, n=2000):
    g, p, tg, _ = frames()
    inp = F.Inputs(g, p, tg, NO_INJ)
    fit = F.base_fit(inp, pads, F.Params(), PLAY, [2021])
    eng = F.Engine(inp, fit)
    d = eng.predict(g.game_id[-6:])
    return [SM.sim_game(rows, fit, n, seed=i) for i, (_, rows) in enumerate(d.groupby("game_id"))]


def test_sum_constraints_in_every_simulated_game(pads):
    for s in _sims(pads):
        assert all(s.checks.values()), s.checks
        h = s.players.is_home.to_numpy()
        pts = np.nan_to_num(s.draws["pts"])
        assert np.array_equal(pts[:, h].sum(1), s.home_pts)
        assert np.array_equal(pts[:, ~h].sum(1), s.away_pts)
        m = np.nan_to_num(s.draws["minutes"])
        assert np.allclose(m[:, h].sum(1), 240 + 25 * s.ot)


def test_distributions_and_probabilities(pads):
    s = _sims(pads, 4000)[0]
    assert len(s.margin) == 4000
    p, push = s.p_cover(-0.5)
    assert 0 < p < 1 and push == 0
    assert abs(s.p_home_win() - ((s.margin > 0).mean() + 0.5 * (s.margin == 0).mean())) < 1e-12
    d = s.player_dist(0, "pts")
    assert d["q10"] <= d["median"] <= d["q90"]
    over, _ = s.player_p_over(0, "pts", d["median"] - 0.5)
    assert over >= 0.5


def test_ten_thousand_is_the_default():
    assert SM.N_SIMS >= 10_000
