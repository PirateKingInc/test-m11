"""Group 8: market at the prediction point."""

import pytest

from betdata.nfl.features.market import Market
from betdata.nfl.points import point_time
from betdata.nfl.store import AsOf
from tests.nfl.synth import make_store


@pytest.fixture(scope="module")
def store():
    return make_store()


def game(store, **where):
    g = store.games
    for k, v in where.items():
        g = g[g[k] == v]
    return g.iloc[0]


def test_market_uses_the_latest_line_before_the_point(store):
    g = game(store, season=2020, week=4)
    t = point_time("final_report", g.kickoff)
    o = store.frame("odds").df
    o = o[(o.game_id == g.game_id) & (o.market == "spreads") & (o.known_at <= t)]
    f = Market().compute(AsOf(store, t), g, "final_report")
    assert f["m_mkt_line"] == -o.line.iloc[-1]
    assert f["m_mkt_move"] == pytest.approx(o.line.iloc[0] - o.line.iloc[-1])
