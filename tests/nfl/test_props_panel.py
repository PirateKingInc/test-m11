"""M15 NFL prop panel: features are as of the point (a later result never changes them), and
teammates' missing targets are reassigned by share with team totals preserved."""

from __future__ import annotations

import numpy as np
import pandas as pd

from betdata.nfl.props import data as D
from betdata.nfl.props.panel import Panel, point_times, redistribute, teammate_context
from tests.nfl.synth import make_store

ROSTER = (("QB", 0), ("RB", 0), ("WR", 0), ("WR", 1), ("WR", 2), ("TE", 0))


def synth_box(store, seed: int = 3) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    g = store.games
    rows = []
    for r in g.itertuples(index=False):
        for team in (r.home, r.away):
            for pos, j in ROSTER:
                tgt = 0 if pos == "QB" else int(rng.poisson({"WR": 7 - 2 * j, "TE": 5,
                                                             "RB": 3}[pos]))  # fmt: skip
                car = int(rng.poisson(15)) if pos == "RB" else int(rng.poisson(2)) * (pos == "QB")
                rec = int(rng.binomial(tgt, 0.65))
                rows.append(dict(
                    nid=f"g{r.game_id}", game_id=r.game_id, team_id=team,
                    player_id=f"P{team}-{pos}{j}", name=f"P{team}-{pos}{j}", pos=pos,
                    att=float(rng.poisson(34)) if pos == "QB" else 0.0, cmp=0.0,
                    pass_yds=float(rng.normal(240, 60)) if pos == "QB" else 0.0, car=float(car),
                    rush_yds=float(car * rng.normal(4.2, 1)), rush_td=float(rng.poisson(0.1)),
                    tgt=float(tgt), rec=float(rec), rec_yds=float(rec * rng.normal(11, 3)),
                    rec_td=float(rng.poisson(0.1)), air=float(tgt * 8), rz_tgt=0.0, rz_car=0.0,
                    routes=0.0, routes_man=0.0, routes_zone=0.0, yds_man=0.0, yds_zone=0.0,
                    snap=0.9, has_part=False, played=True))  # fmt: skip
    b = pd.DataFrame(rows)
    D._context(b, g)
    return b, D._team(b, pd.DataFrame(), {}, g)


def side_for(g) -> pd.DataFrame:
    s = point_times(g, g.index)[["game_id", "point"]]
    return s.assign(pred_margin=1.0, pred_total=44.0, wind=0.0, rain=0.0)


def no_mates() -> pd.DataFrame:
    return pd.DataFrame(columns=["game_id", "point", "team_id", "player_id", "p_play", "bucket",
                                 "mis_tgt", "mis_car", "mis_att", "out_names"])  # fmt: skip


def test_later_results_never_change_inputs():
    store = make_store(seasons=(2019, 2020), weeks=8)
    box, team = synth_box(store)
    g = store.games
    gid = g[(g.season == 2020) & (g.week == 5)].index[0]
    tg = box[box.game_id == gid][["game_id", "team_id", "player_id"]].merge(
        point_times(g, [gid]), on="game_id")  # fmt: skip
    t = tg.t.min()
    f1 = Panel(g, box, team).features(tg, side_for(g), no_mates())
    # Scramble every stat known after the earliest point, then rebuild.
    later = box.known_at > t
    box2 = box.copy()
    rng = np.random.default_rng(9)
    for c in ("tgt", "rec", "rec_yds", "car", "rush_yds", "att", "pass_yds", "td", "snap"):
        box2.loc[later, c] = rng.uniform(0, 300, later.sum())
    team2 = D._team(box2, pd.DataFrame(), {}, g)
    f2 = Panel(g, box2, team2).features(tg, side_for(g), no_mates())
    num = [c for c in f1.columns if pd.api.types.is_numeric_dtype(f1[c])]
    early = (tg.t == t).to_numpy()
    pd.testing.assert_frame_equal(f1.loc[early, num], f2.loc[early, num])
    assert f1.has_state.all() and (f1.l6_tgt > 0).any()


def test_redistribution_keeps_team_totals():
    shares = np.array([0.30, 0.20, 0.15, 0.10, 0.05])
    q = np.array([1.0, 0, 0, 0, 0])  # WR1 surely out
    mis = redistribute(shares, q)
    adj = shares / (1 - mis)
    assert mis[0] == 0 and np.allclose(mis[1:], 0.30 / shares.sum())
    assert np.isclose(adj[1:].sum(), shares.sum())  # the others absorb his share exactly
    assert np.allclose(redistribute(shares, np.zeros(5)), 0)


def test_wr1_out_raises_teammates_shares():
    store = make_store(seasons=(2019, 2020), weeks=8)
    box, team = synth_box(store)
    g = store.games
    gid = g[(g.season == 2020) & (g.week == 6)].index[0]
    home = g.at[gid, "home"]
    slots = point_times(g, [gid])
    panel = Panel(g, box, team)
    ids = [f"P{home}-{p}{j}" for p, j in ROSTER]
    avail = pd.concat([pd.DataFrame({"game_id": gid, "point": pt, "team_id": home,
                                     "player_id": ids, "group": [p for p, _ in ROSTER],
                                     "role": 0.9, "bucket": "fr|-|-", "p_play": 0.9})
                       for pt in slots.point])  # fmt: skip
    healthy = {"final_report": 0.9, "prekick": 0.9, "early": 0.9}
    base = teammate_context(avail, panel.states, slots, healthy)
    wr1 = f"P{home}-WR0"
    out = avail.assign(p_play=np.where(avail.player_id == wr1, 0.0, 0.9),
                       bucket=np.where(avail.player_id == wr1, "fr|Out|DNP", "fr|-|-"))
    hurt = teammate_context(out, panel.states, slots, healthy)
    assert np.allclose(base.mis_tgt, 0)
    others = hurt[hurt.player_id != wr1]
    assert (others.mis_tgt > 0).all()
    assert all("P" in n for names in others.out_names for n in names)
    # Expected shares if they play: the team's target shares still add up.
    tg = others[others.point == "prekick"][["game_id", "team_id", "player_id", "point"]].merge(
        slots, on=["game_id", "point"])  # fmt: skip
    f = panel.features(tg, side_for(g), hurt)
    adj = f.l6_tgt_share / (1 - f.mis_tgt)
    st = panel.states[panel.states.known_at <= tg.t.iloc[0]].groupby("player_id").last()
    total = st.loc[st.team_id == home, "l6_tgt_share"].sum()
    assert np.isclose(adj.sum(), total)
