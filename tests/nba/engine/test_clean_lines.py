"""M20 evaluation: stored prop lines that are not a real main market are dropped."""

import pandas as pd

from betdata.nba.engine.evaluate import clean_lines


def test_broken_two_way_and_mislabeled_lines_are_dropped():
    p = pd.DataFrame({"game_id": range(1, 13), "player_id": "7", "played": True,
                      "tip": pd.date_range("2023-01-01", periods=12, tz="UTC"),
                      "reb": [3.0] * 12, "pts": 10.0, "ast": 2.0, "fg3m": 1.0, "pra": 15.0})
    pv = pd.DataFrame({"game_id": [12, 12, 12], "player_id": "7", "market": "rebounds",
                       "line": [3.5, 3.5, 14.5], "over_price": [-110, 1600, -115],
                       "under_price": [-110, 1200, -105]})  # fmt: skip
    out = clean_lines(pv, p)
    assert out.over_price.tolist() == [-110]  # +1600/+1200 and a 14.5 rebound line dropped
