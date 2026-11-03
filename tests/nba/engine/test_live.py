"""M20 Parts E and G: the live document, pricing lines from it, and the pick rule."""

from datetime import timedelta

import numpy as np

from betdata.nba.engine import fitting as F
from betdata.nba.engine import live as L
from betdata.nba.engine import picks as K
from tests.nba.engine.conftest import PLAY
from tests.nba.engine.league import NO_INJ, frames

DESIGN = K.Design({"spreads": 0.5, "totals": 0.5, "h2h": 0.5, "points": 0.3,
                   "rebounds": 0.3, "assists": 0.3, "threes": 0.3},
                  {}, {"game": 0.03, "prop": 0.05}, {"game": False, "prop": False})  # fmt: skip


def _doc(pads, n=3000):
    g, p, tg, _ = frames()
    eng = F.Engine(F.Inputs(g, p, tg, NO_INJ), F.base_fit(F.Inputs(g, p, tg, NO_INJ), pads,
                                                         F.Params(), PLAY, [2021]))  # fmt: skip
    gid = int(g.game_id.iloc[-1])
    now = g.tip[gid].to_pydatetime() - timedelta(hours=2)
    return L.predict_live(None, now, eng, [gid], n_sims=n), gid


def test_live_document_has_distributions_and_passing_checks(pads):
    doc, gid = _doc(pads)
    [g] = doc["games"]
    assert g["game_id"] == gid and all(g["checks"].values())
    assert abs(sum(g["margin"]["p"]) - 1) < 1e-3 and abs(sum(g["total"]["p"]) - 1) < 1e-3
    pl = next(x for x in g["players"] if x["cdf"]["pts"])
    assert pl["cdf"]["pts"][-1] == 1.0 and np.all(np.diff(pl["cdf"]["pts"]) >= -1e-9)


def test_p_over_from_cdf():
    cdf = [0.1, 0.3, 0.6, 1.0]  # P(X<=0..3)
    assert np.allclose(L.p_over(cdf, 1.5), (0.7, 0.0))
    assert np.allclose(L.p_over(cdf, 2), (0.4, 0.3))  # push at exactly 2


def test_pick_rule_one_best_pick_per_market_and_threshold(pads):
    doc, gid = _doc(pads)
    game = doc["games"][0]
    m = game["mu_margin"]
    line = -round(m) - 6.5  # the home side priced far too short: the away side is value
    two = {"spreads": {"draftkings": (line, -110, -110), "fanduel": (line, -105, -115)},
           "totals": {"draftkings": (round(game["mu_total"]) + 0.5, -110, -110)}}
    cands = K.game_candidates(game, two, DESIGN)
    picks = K.best_picks(cands, DESIGN)
    sp = [c for c in picks if c.market == "spreads"]
    # away -110 at DraftKings beats -115 at FanDuel
    assert len(sp) == 1 and sp[0].side == "away" and sp[0].book == "draftkings"
    assert all(c.ev >= DESIGN.theta["game"] for c in picks)
    assert sp[0].book_nv is not None and abs(sp[0].fair - (0.5 * sp[0].p_engine
                                                          + 0.5 * sp[0].p_market)) < 1e-9
    assert K.dedup_key(gid, sp[0]) == f"engine:{gid}:spreads:"


def test_props_priced_from_the_players_cdf(pads):
    doc, _ = _doc(pads)
    game = doc["games"][0]
    pl = max(game["players"], key=lambda x: x["mean"]["pts"] or 0)
    name = pl["name"].lower()
    med = pl["median"]["pts"]
    props = {(name, "points"): {"draftkings": (med + 8.5, -110, -110)}}
    picks = K.best_picks(K.prop_candidates(game, props, DESIGN), DESIGN)
    assert len(picks) == 1 and picks[0].side == "under" and picks[0].player == pl["name"]
