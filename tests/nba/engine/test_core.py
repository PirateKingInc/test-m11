"""M20 engine core: stints-derived facts, recency sums, as-of harness, outlier and role-change
fixtures, team-consistent means (MODEL.md "M20 B1-B7")."""

import numpy as np
import pandas as pd

from betdata.nba.engine import fitting as F
from betdata.nba.engine.states import Params
from tests.nba.engine.conftest import PLAY
from tests.nba.engine.league import NO_INJ, frames, league


def _engine(g, p, tg, pads, prm=None, inj=NO_INJ):
    inp = F.Inputs(g, p, tg, inj)
    fit = F.base_fit(inp, pads, prm or Params(), PLAY, [2021])
    return F.Engine(inp, fit)


def test_minutes_sum_to_the_team_and_shots_to_its_volume(pads):
    g, p, tg, _ = frames()
    eng = _engine(g, p, tg, pads)
    d = eng.predict(g.game_id[-5:])
    for _, r in d.groupby(["game_id", "team_id"]):
        assert abs((r.p_play * r.minutes).sum() / (240 * (1 + eng.fit.p_ot * 5 / 48)) - 1) < 0.08
        use = ((r.fg2a_m + r.fg3a_m + 0.44 * r.fta_m + r.tov_m) * r.p_play).sum()
        poss = (r.pace_own * r.pace_opp / r.league_pace).iloc[0] * (1 + eng.fit.p_ot * 5 / 48)
        assert abs(use / poss / r.use_team.iloc[0] - 1) < 0.08


def test_asof_harness_later_rows_never_change_an_earlier_prediction(pads):
    """Rebuilt from (i) all data, (ii) only rows known by the point, (iii) all data with every
    later row scrambled: the predictions of a game are identical."""
    from betdata.nba.engine import data as E

    g, p, st = league()
    target = int(g.game_id.iloc[40])
    t = g.tip[target]
    cols = ["player_id", "minutes", "pts_m", "reb_m", "ast_m", "fg3m_m"]

    g0, p0, tg0 = E.build(g, p.copy(), st)
    fit = _engine(g0, p0, tg0, pads).fit  # training constants: fixed before the point

    def run(pp, ss):
        g2, p2, tg2 = E.build(g, pp.copy(), ss)
        out = F.Engine(F.Inputs(g2, p2, tg2, NO_INJ), fit).predict([target])
        return out[cols].sort_values("player_id").reset_index(drop=True)

    full = run(p, st)
    known = p.known_at <= t - pd.Timedelta(hours=1)
    # (ii) later games' rows removed (the target's own box is a post-game fact, kept out too)
    cut = run(p[known], st[st.known_at <= t - pd.Timedelta(hours=1)])
    later = ~known
    scr = p.copy()
    rng = np.random.default_rng(0)
    for c in ("minutes", "points", "fga", "ast", "dreb", "fg3a", "starter"):
        scr.loc[later, c] = rng.permutation(scr.loc[later, c].to_numpy())
    sst = st.copy()
    sl = sst.known_at > t - pd.Timedelta(hours=1)
    sst.loc[sl, "home_pts"] = rng.permutation(sst.loc[sl, "home_pts"].to_numpy())
    scrambled = run(scr, sst)
    # (ii) has no box rows for the target, so it has no candidates from them: compare players
    # common to all three (the rotation comes from earlier games)
    for o in (cut, scrambled):
        m = full.merge(o, on="player_id", suffixes=("", "_o"))
        assert len(m) >= 16
        for c in cols[1:]:
            assert np.allclose(m[c], m[f"{c}_o"], atol=1e-9), c


def test_blowout_minutes_are_down_weighted_in_the_next_projection(pads):
    """A starter who sat a non-competitive fourth quarter: with the blowout weight below 1 his
    next projection leans less on that game."""
    def mins(team, pid, day):
        base = 34 - 2.5 * int(pid[-2:])
        return 20.0 if (day == 40 and pid == "100") else base
    g, p, tg, _ = frames(minutes=mins, blowouts=(40,))
    nxt = g.game_id[(g.tip > g.tip.iloc[40]) & ((g.home == 1) | (g.away == 1))].iloc[0]
    prm_full, prm_low = Params(h_min_stable=3, h_min_change=3), Params(h_min_stable=3,
                                                                      h_min_change=3)  # fmt: skip
    prm_low.w["blowout"] = 0.25
    a = _engine(g, p, tg, pads, prm_full).predict([nxt])
    b = _engine(g, p, tg, pads, prm_low).predict([nxt])
    ma = a.set_index("player_id").minutes["100"]
    mb = b.set_index("player_id").minutes["100"]
    assert mb > ma + 0.5


def test_new_team_bigger_role_projects_above_the_old_role(pads):
    """A Schroder-like move: a bench player (22 min) traded into a starting role (34 min).
    After three games there his projection is well above his old role's minutes."""
    def mins(team, pid, day):
        if pid == "305":
            return 34.0 if day >= 30 else 22.0
        if pid == "200" and day >= 30:
            return 0.0  # the new team's starter he replaces is gone
        return 34 - 2.5 * int(pid[-2:])
    g, p, tg, _ = frames(minutes=mins, moves={"305": (30, 2)})
    games2 = g[(g.tip > g.tip.iloc[29]) & ((g.home == 2) | (g.away == 2))].game_id
    target = games2.iloc[3]
    eng = _engine(g, p, tg, pads, Params(h_min_stable=10, h_min_change=3))
    d = eng.predict([target]).set_index("player_id")
    old = p[(p.player_id == "305") & (p.season == 2021) & (p.team_id == 3)].minutes.mean()
    assert d.loc["305", "role_change"]
    assert d.loc["305", "minutes"] > old + 3
    # the same player without role-change detection (one long half-life) projects lower
    eng2 = _engine(g, p, tg, pads, Params(h_min_stable=10, h_min_change=10))
    assert d.loc["305", "minutes"] > eng2.predict([target]).set_index("player_id").loc[
        "305", "minutes"]  # fmt: skip
