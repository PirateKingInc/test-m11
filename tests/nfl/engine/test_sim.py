"""M17 simulation on a hand-built drive model (no data): identities, script, availability."""

import numpy as np
import pytest

from betdata.nfl.engine import drives as D
from betdata.nfl.engine import sim as SM


def _model(td=0.25, fg=0.15) -> D.DriveModel:
    k = len(D.features([75], [0], [0], [900], [1], [2700], [1])[0])
    probs = np.array([td, fg, 0.38, 0.1, 0.05, 0.05, 0.015, 0.005])
    inter = np.log(probs / probs.sum())
    coef = np.zeros((len(D.OUTCOMES), k))
    coef[0, 2] = 8.0  # better matchup, more touchdowns
    coef[0, 10] = 0.1  # home field
    templ = {}
    for o in D.OUTCOMES:
        for b in range(len(D.BUCKETS) - 1):
            plays = {"td": 8, "fg": 8, "punt": 4}.get(o, 3)
            yards = {"td": 70, "fg": 45, "punt": 12}.get(o, 10)
            templ[f"{o}|{b}"] = np.array([[plays, 30.0 * plays, yards, 40.0, 70.0, 0.6, 75.0]])
    return D.DriveModel(coef, inter, np.zeros(7), 0.4, (6.0, -0.11), templ,
                        np.zeros(len(D.OUTCOMES)), 1.0)


def _side(x=0.0, out=0.0, t="h"):
    side = SM.Side(x=x, offset=0.0, pace=1.0, sack=0.06, scramble=0.04, ypa=7.0, ypc=4.3,
                   pass_td_share=0.6, qbs=[("qb1", 1 - out, 0.0), ("qb2", 1.0, -0.05)],
                   rec=[("wr1", 0.0, 0.25, 0.65, 13.0, 0.25), ("wr2", 0.1, 0.18, 0.62, 12.0, 0.2),
                        ("te1", 0.0, 0.15, 0.7, 10.0, 0.25), ("rb1", 0.0, 0.1, 0.8, 7.0, 0.1)],
                   rush=[("rb1", 0.0, 0.65, 4.3, 0.6), ("rb2", 0.0, 0.25, 4.1, 0.25),
                         ("qb1", 0.0, 0.08, 5.0, 0.1)])  # fmt: skip
    # player ids are unique within a game (gsis): prefix the team
    side.qbs = [(f"{t}{q[0]}", *q[1:]) for q in side.qbs]
    side.rec = [(f"{t}{r[0]}", *r[1:]) for r in side.rec]
    side.rush = [(f"{t}{r[0]}", *r[1:]) for r in side.rush]
    return side


PAT = {"xp": 0.94, "two_rate": 0.05, "two_made": 0.48}
KICK = np.full(50, 75.0)


@pytest.fixture(scope="module")
def game():
    home, away = _side(0.05), _side(-0.05, t="a")
    return SM.simulate(_model(), home, away, KICK, PAT, SM.SimParams(n=4000), seed=1), home, away


def test_identities_hold_in_every_simulation(game):
    gs, home, away = game
    bad = {k: v for k, v in SM.consistency(gs, home, away).items() if v > 1e-6}
    assert bad == {}


def test_better_matchup_and_home_field_win_more(game):
    gs, *_ = game
    assert gs.margin.mean() > 1.0
    assert 0.5 < (gs.margin > 0).mean() < 0.95


def test_same_seed_same_result():
    a = SM.simulate(_model(), _side(), _side(t="a"), KICK, PAT, SM.SimParams(n=500), seed=3)
    b = SM.simulate(_model(), _side(), _side(t="a"), KICK, PAT, SM.SimParams(n=500), seed=3)
    assert np.array_equal(a.margin, b.margin)


def test_a_missing_receiver_gets_nothing_and_teammates_gain():
    m = _model()
    base = SM.simulate(m, _side(), _side(t="a"), KICK, PAT, SM.SimParams(n=3000), seed=5)
    out = _side()
    out.rec[0] = ("hwr1", 1.0, 0.25, 0.65, 13.0, 0.25)  # surely out
    hurt = SM.simulate(m, out, _side(t="a"), KICK, PAT, SM.SimParams(n=3000), seed=5)
    assert hurt.players[("hwr1", "targets")].sum() == 0
    assert (hurt.players[("hwr2", "targets")].mean()
            > base.players[("hwr2", "targets")].mean() + 0.5)


def test_leaders_run_more_in_the_second_half():
    """Correlated script: in sims where the home side wins big, it ran a larger share."""
    gs = SM.simulate(_model(), _side(0.1), _side(-0.1, t="a"), KICK, PAT, SM.SimParams(n=4000), 2)
    share = gs.team[(0, "runs")] / np.maximum(gs.team[(0, "plays")], 1)
    big = gs.margin >= 14
    assert share[big].mean() > share[gs.margin <= -14].mean()
