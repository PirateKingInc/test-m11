"""Group 7: game context."""

import pandas as pd

from betdata.nfl.features.context import GameContext, _records
from betdata.nfl.points import point_time
from betdata.nfl.store import AsOf
from tests.nfl.synth import make_store


def test_records_count_only_regular_season_results():
    games = pd.DataFrame({"season": [2020, 2020], "season_type": ["REG", "POST"],
                          "home": [1, 1], "away": [2, 2]}, index=[10, 11])  # fmt: skip
    results = pd.DataFrame({"game_id": [10, 11], "home_score": [20, 30], "away_score": [10, 0]})
    assert _records(results, games) == {(2020, 1): (1.0, 1), (2020, 2): (0.0, 1)}


def test_division_and_final_week_flags():
    store = make_store()
    g = store.games
    last = g[(g.season == 2020) & (g.week == 10)].iloc[0]
    f = GameContext().compute(AsOf(store, point_time("final_report", last.kickoff)), last,
                              "final_report")  # fmt: skip
    assert f["t_ctx_div"] == float(bool(last.div_game))
    assert set(f) >= {"m_ctx_race", "m_ctx_out", "m_ctx_rest"}
