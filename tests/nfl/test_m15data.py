"""M15 NFL inputs: the expected-rate models and the regressed team rates."""

import numpy as np
import pandas as pd
import pytest

from betdata.nfl.features.m15_common import team_rates
from betdata.nfl.m15data import Logistic, red_zone_trips, third_downs


def test_logistic_recovers_fg_make_probability_by_distance():
    rng = np.random.default_rng(0)
    dist = rng.uniform(18, 60, 20000)
    true = Logistic(6.0, -0.11)
    made = rng.random(len(dist)) < true.prob(dist)
    fit = Logistic.fit(dist, made)
    assert abs(fit.b0 - 6.0) < 0.4 and abs(fit.b1 + 0.11) < 0.01
    p = fit.prob([20, 40, 55])
    assert p[0] > p[1] > p[2] and p[0] > 0.97


def test_third_down_expectation_falls_with_distance():
    rng = np.random.default_rng(1)
    togo = rng.integers(1, 16, 5000)
    conv = rng.random(5000) < 1 / (1 + np.exp(-(0.9 - 0.8 * np.log(togo))))
    pbp = pd.DataFrame({"down": 3, "play_type": "pass", "ydstogo": togo,
                        "two_point_attempt": 0.0, "third_down_converted": conv.astype(float)})
    d = third_downs(pbp)
    fit = Logistic.fit(d.x, d.conv)
    assert abs(fit.b1 + 0.8) < 0.1
    assert fit.prob(np.log(1)) > 0.65 > 0.3 > fit.prob(np.log(10))


def test_red_zone_trip_counted_once_from_its_first_snap():
    pbp = pd.DataFrame({"game_id": "g", "fixed_drive": [1, 1, 1, 2], "play_id": [1, 2, 3, 4],
                        "yardline_100": [30, 18, 4, 12], "play_type": "run", "posteam": "A",
                        "fixed_drive_result": ["Touchdown"] * 3 + ["Field goal"]})
    t = red_zone_trips(pbp)
    assert list(t.x) == [18.0, 12.0] and list(t.td) == [1.0, 0.0]


def test_team_rates_regress_small_samples_to_the_league():
    k = pd.Timestamp("2024-10-01", tz="UTC")
    df = pd.DataFrame({"team": [1, 2, 3], "opp": [2, 3, 1], "kickoff": k,
                       "x": [10.0, 1.0, 50.0], "n": [100.0, 10.0, 100.0]})
    r = team_rates(df, {"rate": ("x", "n", 50.0, "team")})
    league = 61 / 210
    assert r[2]["rate"] == pytest.approx((1 + 50 * league) / 60 - league)
    assert abs(r[2]["rate"]) < abs(0.1 - league)  # pulled most of the way back
    allowed = team_rates(df, {"rate": ("x", "n", 50.0, "opp")})
    assert allowed[3]["rate"] == pytest.approx(r[2]["rate"])  # team 3 faced team 2's offense
