"""Group 4: environment."""

from datetime import timedelta

import pandas as pd
import pytest

from betdata.nfl.features.environment import Environment
from betdata.nfl.points import point_time
from betdata.nfl.store import AsOf, Frame, Store
from tests.nfl.synth import make_store


@pytest.fixture(scope="module")
def store():
    return make_store()


def game(store, **where):
    g = store.games
    for k, v in where.items():
        g = g[g[k] == v]
    return g.iloc[0]


def test_environment_dome_and_forecast_timing(store):
    dome = game(store, roof_type="dome")
    t = point_time("prekick", dome.kickoff)
    assert Environment().compute(AsOf(store, t), dome, "prekick")["t_env_indoor"] == 1.0
    out = game(store, roof_type="outdoors")
    t = point_time("final_report", out.kickoff)
    w = store.frame("weather").df
    windy = pd.DataFrame([dict(game_id=out.game_id, temp_c=10.0, wind_kph=40.0, gust_kph=50.0,
                               precip_mm=0.0, known_at=t + timedelta(minutes=5))])  # fmt: skip
    later = Store(store.games, {**store.frames, "weather": Frame(pd.concat([w, windy]))})
    a = Environment().compute(AsOf(store, t), out, "final_report")
    b = Environment().compute(AsOf(later, t), out, "final_report")
    assert a == b  # a forecast fetched after the point is not seen
    c = Environment().compute(AsOf(later, t + timedelta(minutes=10)), out, "final_report")
    assert c["t_env_wind"] == 30.0
