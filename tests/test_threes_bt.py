"""Threes research backtest (betdata/nba/props/threes_bt.py): the replicated OG Ranking v2 rule,
and no leakage. Research only."""

import inspect

import numpy as np
import pandas as pd
import pytest

from betdata.nba.props import threes_bt as TB


def _priced(rows):
    d = pd.DataFrame(rows, columns=["game_id", "player_id", "p", "nv", "line", "over_price",
                                    "under_price", "median", "over", "day", "season", "m_hat"])
    return d


def test_candidates_use_the_market_threshold_and_side():
    d = _priced([
        (1, "a", 0.60, 0.51, 2.5, -110, -110, 3.0, 1.0, "d", 2023, 30),  # +9 pp over: threes in
        (1, "b", 0.58, 0.51, 2.5, -110, -110, 3.0, 1.0, "d", 2023, 30),  # +7 pp: below 8
        (1, "c", 0.40, 0.50, 2.5, -110, -110, 2.0, 0.0, "d", 2023, 30),  # under by 10 pp
    ])
    c = TB.candidates(d, "threes")
    assert list(c.player_id) == ["a", "c"] and list(c.side) == ["over", "under"]
    assert list(c.result) == ["win", "win"]
    assert c.ps.round(2).tolist() == [0.60, 0.60]
    c6 = TB.candidates(d, "assists")  # F's live 6 pp
    assert list(c6.player_id) == ["a", "b", "c"]


@pytest.mark.parametrize("price,ok", [(-150, True), (130, True), (-151, False), (131, False)])
def test_price_range_bounds(price, ok):
    d = _priced([(1, "a", 0.62, 0.50, 2.5, price, -110, 3.0, 1.0, "d", 2023, 30)])
    e = TB.ranking_v2(TB.candidates(d, "threes"))
    assert (len(e) == 1) is ok


def test_median_strictly_on_the_side_and_one_per_player_game_market():
    d = _priced([
        (1, "eq", 0.62, 0.50, 2.5, -110, -110, 2.5, 1.0, "d", 2023, 30),  # median = line: out
        (1, "under_ok", 0.38, 0.50, 2.5, -110, -110, 2.0, 0.0, "d", 2023, 30),
        (2, "x", 0.66, 0.50, 1.5, 120, -140, 2.0, 1.0, "d", 2023, 30),
        (2, "x", 0.64, 0.50, 2.5, 110, -130, 3.0, 1.0, "d", 2023, 30),  # same pgm: lower p out
    ])
    e = TB.ranking_v2(TB.candidates(d, "threes"))
    assert sorted(e.player_id) == ["under_ok", "x"]
    assert e[e.player_id == "x"].line.iloc[0] == 1.5


def test_rank_by_probability_ties_shorter_price_top_n_per_day():
    d = _priced([
        (1, "a", 0.62, 0.50, 2.5, 120, -140, 3.0, 1.0, "d1", 2023, 30),
        (2, "b", 0.62, 0.50, 2.5, -110, -110, 3.0, 0.0, "d1", 2023, 30),
        (3, "c", 0.70, 0.50, 2.5, 100, -120, 3.0, 1.0, "d1", 2023, 30),
        (4, "d", 0.61, 0.50, 2.5, -110, -110, 3.0, 1.0, "d2", 2023, 30),
    ])
    e = TB.ranking_v2(TB.candidates(d, "threes"))
    t = TB.top_per_day(e, 2)
    assert t[t.day == "d1"].player_id.tolist() == ["c", "b"]  # tie at 62%: -110 before +120
    assert t[t.day == "d2"].player_id.tolist() == ["d"]


def test_walkforward_platt_never_sees_the_scored_season():
    rng = np.random.default_rng(0)
    rows = []
    for s in (2022, 2023, 2024, 2025):
        for _ in range(400):
            ps = rng.uniform(0.5, 0.7)
            rows.append({"season": s, "ps": ps, "result": "win" if rng.random() < ps else "loss"})
    c = pd.DataFrame(rows)
    base = TB.platt_walkforward(c)
    # poison every result from 2025 on: the 2025 fit (earlier seasons only) does not move
    c2 = c.copy()
    c2.loc[c2.season >= 2025, "result"] = "loss"
    again = TB.platt_walkforward(c2)
    assert again[2025] == base[2025] and again[2023] == base[2023]
    assert base[2023]["n"] == 400 and base[2025]["n"] == 1200


def test_teammates_rule_reads_the_prediction_point(monkeypatch):
    """The live helper is called at each row's prediction time (its as-of reads are tested in
    tests/test_rules_v2.py), never later."""
    from betdata import eligibility as EL

    seen = []
    monkeypatch.setattr(EL, "teammates_out",
                        lambda m, g, t, pl, at: seen.append((g, pl, at)) or 2)  # fmt: skip

    class Eng:
        def connect(self):
            import contextlib

            return contextlib.nullcontext(None)

    rows = pd.DataFrame({"game_id": [7], "team_id": [3], "player_id": ["9"],
                         "t": [pd.Timestamp("2024-01-05 18:00", tz="UTC")]})
    assert TB.teammates_out(Eng(), rows, {"9": "A Guy"}) == [2]
    (g, pl, at) = seen[0]
    assert (g, pl) == (7, "A Guy") and at.isoformat() == "2024-01-05T18:00:00+00:00"


def test_the_model_is_fitted_before_the_test_seasons():
    from betdata.nba.pipeline import TEST, TRAIN, VALIDATE

    assert not set(TRAIN) & set(TEST) and not set(VALIDATE) & set(TEST)
    assert set(TB.TEST_SEASONS) == set(TEST)
    src = inspect.getsource(TB.load)
    assert "f.season.isin((*TRAIN, *VALIDATE))" in src


def test_verdicts():
    assert TB.verdict({"w": 10, "l": 5, "roi": 0.1, "win": 0.6},
                      {"win": 0.5}) == "too few picks to tell"
    assert TB.verdict({"w": 40, "l": 50, "roi": -0.05, "win": 0.44},
                      {"win": 0.5}) == "held up on old seasons"
    assert TB.verdict({"w": 60, "l": 40, "roi": 0.1, "win": 0.6},
                      {"win": 0.5}) == "did not hold up"
