"""M17 rate store: as-of reads, season carry-over and shrinkage (synthetic, no data)."""

import numpy as np
import pandas as pd

from betdata.nfl.engine import drives as D
from betdata.nfl.engine.rates import RateParams, post_game_state, read_state


def _games():
    ko = pd.to_datetime(["2019-12-01", "2020-09-13", "2020-09-20", "2020-09-27"], utc=True)
    return pd.DataFrame({"unit": "KC", "season": [2019, 2020, 2020, 2020], "kickoff": ko,
                         "known_at": ko + pd.Timedelta(hours=12), "x": [10.0, 1.0, 2.0, 4.0]})


def test_a_read_sees_only_games_known_at_the_instant():
    p = RateParams(half_life=1.0, last_season_w=0.5)
    st = post_game_state(_games(), ["x"], p)
    q = pd.DataFrame({"unit": ["KC"] * 3, "season": [2020] * 3,
                      "t": pd.to_datetime(["2020-09-13 20:00", "2020-09-14 01:00",
                                           "2020-09-26 00:00"], utc=True)})  # fmt: skip
    got = read_state(st, q, ["x"], p).x.tolist()
    # before the first 2020 game is known: last season's 10 carried at 0.5; then 5*0.5+1;
    # then (3.5)*0.5+2
    assert got == [5.0, 3.5, 3.75]


def test_seasons_older_than_last_are_dropped():
    p = RateParams(half_life=1.0, last_season_w=0.5)
    st = post_game_state(_games(), ["x"], p)
    q = pd.DataFrame({"unit": ["KC"], "season": [2022],
                      "t": pd.to_datetime(["2022-09-01"], utc=True)})
    assert read_state(st, q, ["x"], p).x.tolist() == [0.0]


def test_drive_buckets_and_features():
    assert D.bucket(np.array([5, 25, 75, 99])).tolist() == [0, 2, 8, 10]
    X = D.features([75, 20], [0.0, 0.1], [0, 7], [900, 100], [1, 2], [2700, 100], [1, 0])
    assert X.shape == (2, 16)
    assert X[1, 12] == 1.0  # leading late in the second half: the kneel-down term
