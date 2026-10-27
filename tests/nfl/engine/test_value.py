"""M17 Part D value layer: P(cover), picks at -110, threshold choice, the three tests."""

import numpy as np
import pandas as pd

from betdata.nfl.engine import value as V


def test_p_cover_from_the_margin_histogram():
    hist = np.zeros(121)
    hist[60 + 3] = 50  # home by 3
    hist[60 - 7] = 50  # away by 7
    assert V.p_cover(hist, -2.5) == 0.5
    assert V.p_cover(hist, -3.0) == 0.0  # the push is excluded: only the losses remain
    assert V.p_cover(hist, 7.5) == 1.0


def _games():
    return pd.DataFrame({
        "margin_mean": [10.0, -5.0, 1.0, 4.0],
        "close_spread_home": [-3.0, -1.0, -1.0, -4.0],  # gaps 7, -6, 0, 0
        "act_margin": [7.0, -2.0, 3.0, 4.0]})  # fmt: skip


def test_side_bets_and_record():
    b = V.side_bets(_games(), 2.0)
    assert len(b) == 2  # two games clear a 2-point gap
    # home -3, won by 7: win; away +1 when home -1, home lost by 2: away covers
    assert b.result.tolist() == [1.0, 1.0]
    r = V.record(b)
    assert (r["wins"], r["losses"], r["pushes"]) == (2, 0, 0)
    assert abs(r["roi"] - 100 / 110) < 1e-9
    push = V.side_bets(_games().assign(close_spread_home=[-7.0, -1.0, -1.0, -4.0]), 2.0)
    assert np.isnan(push.result.iloc[0])  # home -7, won by 7


def test_threshold_needs_enough_picks_and_a_positive_roi():
    rng = np.random.default_rng(0)
    n = 400
    g = pd.DataFrame({"margin_mean": rng.normal(0, 4, n), "close_spread_home": 0.5,
                      "act_margin": rng.normal(0, 13, n)})
    res = V.choose_threshold(g, min_picks=100)
    assert {r["threshold"] for r in res["grid"]} == set(V.SIDE_GRID)
    best = [r for r in res["grid"] if r["threshold"] == res["chosen"]]
    assert res["chosen"] is None or (best[0]["roi"] > 0 and best[0]["picks"] >= 100)


def test_clv_slope_finds_lines_moving_toward_the_engine():
    rng = np.random.default_rng(1)
    gap = rng.normal(0, 3, 500)
    d = pd.DataFrame({"margin_mean": gap, "open_spread": 0.0,
                      "close_spread": -0.3 * gap + rng.normal(0, 0.5, 500)})
    r = V.test_clv(d)
    assert 0.25 < r["slope"] < 0.35
    assert r["toward_engine_gap_ge_2"]["share_toward"] > 0.9


def test_projection_sites_are_not_books():
    from betdata.nfl.engine.lines import NOT_BOOKS

    assert {"consensus", "teamrankings", "numberfire", "accuscore"} <= NOT_BOOKS
