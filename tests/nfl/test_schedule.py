"""Group 3: schedule and travel."""

import pytest

from betdata.nfl.features.schedule import Schedule, miles
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


def test_miles():
    assert miles(39.744, -105.02, 42.091, -71.264) == pytest.approx(1765, rel=0.02)  # DEN-NE
    assert miles(None, 1, 2, 3) == 0.0


def test_schedule_altitude_travel_and_body_clock(store):
    g = game(store, home=3, season=2019)  # team 3 plays at 1,600 m in Denver's time zone
    f = Schedule().compute(AsOf(store, point_time("prekick", g.kickoff)), g, "prekick")
    assert f["m_sched_alt"] == -1.0  # only the visitor is a sea-level team at altitude
    assert f["m_sched_travel"] < 0  # the visitor travelled, the home team didn't
    g_ = store.games
    west = g_[(g_.away == 7) & g_.home.isin([1, 2, 8])].iloc[0]  # a Seattle team in the East
    f2 = Schedule().compute(AsOf(store, west.kickoff), west, "prekick")
    assert f2["m_sched_tz"] == -3.0
