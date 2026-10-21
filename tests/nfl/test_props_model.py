"""M15 NFL prop distributions and P(over): sane, monotone, push-aware; anytime TD = 1 - e^-l;
the Platt step; the fit round-trips through JSON."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from betdata.nba.props.model import Dispersion
from betdata.nfl.props import model as M
from betdata.nfl.props.panel import Panel
from tests.nfl.synth import make_store
from tests.nfl.test_props_panel import no_mates, side_for, synth_box


def test_yards_distributions():
    disp = Dispersion(50.0, 2.0, 0.3)
    for fam in ("negbin", "normal"):
        d = M.YardsDist(fam, disp)
        lines = np.array([20.5, 50.5, 80.5])
        p = d.p_over(np.full(3, 55.0), lines)
        assert np.all((p > 0) & (p < 1)) and np.all(np.diff(p) < 0)
        assert d.p_over([70.0], [50.5])[0] > d.p_over([40.0], [50.5])[0]
        assert d.p_push([55.0], [50.5])[0] == 0
        under = 1 - d.p_over([55.0], [49.0])[0] - d.p_push([55.0], [50.0])[0]
        assert 0 < d.p_push([55.0], [50.0])[0] < 0.05 and 0 < under < 1


def test_anytime_td_and_platt():
    mod = M.PropModel({}, {}, {}, {})
    lam = np.array([0.1, 0.5, 1.0])
    assert np.allclose(mod.p_over("anytime_td", lam, 0.5), 1 - np.exp(-lam))
    mod.calib["anytime_td"] = (1.0, 0.0)
    assert np.allclose(mod.p_over("anytime_td", lam, 0.5), 1 - np.exp(-lam))
    mod.calib["anytime_td"] = (1.0, 0.5)  # shifts every probability up
    assert np.all(mod.p_over("anytime_td", lam, 0.5) > 1 - np.exp(-lam))


def test_fit_predicts_and_round_trips():
    store = make_store(seasons=(2019, 2020), weeks=8)
    box, team = synth_box(store)
    panel = Panel(store.games, box, team)
    tg = panel.played_targets((2019, 2020))
    f = panel.features(tg, side_for(store.games), no_mates())
    y = box.set_index(["game_id", "player_id"])[["pass_yds", "rush_yds", "rec_yds", "rec", "td",
                                                 "tgt", "car", "att"]]  # fmt: skip
    f = f.join(y.rename(columns=lambda c: f"y_{c}"), on=["game_id", "player_id"])
    t = team.set_index(["game_id", "team_id"])[["t_att", "t_tgt", "t_car"]]
    f = f.join(t.rename(columns=lambda c: "yt_" + c[2:]), on=["game_id", "team_id"])
    f = f[f.has_state]
    mod = M.fit(f, families={"rec_yds": "normal"})
    back = M.PropModel.from_dict(json.loads(json.dumps(mod.to_dict())))
    pops = M.populations(f)
    for mk in M.MARKETS:
        d = f[pops[mk]]
        assert len(d) > 0, mk
        mu = back.predict(d, mk)
        assert np.allclose(mu, mod.predict(d, mk)) and (mu >= 0).all()
        p = back.p_over(mk, mu, np.full(len(d), 0.5 if mk == "anytime_td" else 30.5))
        assert ((p >= 0) & (p <= 1)).all()
    assert isinstance(back.dists["rec_yds"], M.YardsDist)
    assert back.dists["rec_yds"].family == "normal"
    assert pd.Series(mod.volumes(f)["tgt"]).between(10, 60).all()
