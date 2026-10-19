"""Group 5: officials."""

import pytest

from betdata.nfl.features.officials import Officials
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


def test_officials_unknown_before_game_day(store):
    g = game(store, season=2020, week=5)
    fr = Officials().compute(AsOf(store, point_time("final_report", g.kickoff)), g,
                             "final_report")  # fmt: skip
    assert fr == {"t_ref_pens": 0.0, "t_ref_points": 0.0, "m_ref_home": 0.0}
    pk = Officials().compute(AsOf(store, point_time("prekick", g.kickoff)), g, "prekick")
    assert any(v != 0.0 for v in pk.values())
