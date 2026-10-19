"""Group 6: coaching tendencies."""

import math

import pandas as pd

from betdata.nfl.features.coaching import Coaching, _coach_rates
from betdata.nfl.points import point_time
from betdata.nfl.store import AsOf
from tests.nfl.synth import make_store


def test_aggressive_coach_rates_above_league():
    coaches = pd.DataFrame({"game_id": [1, 2], "home_coach": ["Bold", "Bold"],
                            "away_coach": ["Shy", "Shy"]})  # fmt: skip
    tg = pd.DataFrame({
        "game_id": [1, 1, 2, 2], "home": [True, False, True, False],
        "fourth_opps": [3.0, 3.0, 3.0, 3.0], "fourth_goes": [3.0, 0.0, 3.0, 0.0],
        "early_neutral_n": [20.0] * 4, "early_neutral_pass_oe": [100.0, -100.0, 100.0, -100.0],
    })  # fmt: skip
    r = _coach_rates(coaches, tg)
    assert r["Bold"]["go"] > 0 > r["Shy"]["go"]
    assert r["Bold"]["pass_oe"] > 0 > r["Shy"]["pass_oe"]


def test_coaching_features_at_the_final_report():
    store = make_store()
    g = store.games[(store.games.season == 2020) & (store.games.week == 10)].iloc[0]
    c = Coaching().compute(AsOf(store, point_time("final_report", g.kickoff)), g, "final_report")
    assert set(c) == {"m_coach_go", "t_coach_go", "m_coach_pass_oe", "t_coach_pass_oe"}
    assert all(math.isfinite(v) for v in c.values())
