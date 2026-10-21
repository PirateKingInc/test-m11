"""M14 selection helpers on small hand-made frames: spread and moneyline paper bets (at the
opening line and at the close), the 95% range, the blend weight and threshold choice."""

import math

import numpy as np
import pandas as pd
import pytest

from betdata.nba.selection import (
    blend_weight,
    choose_threshold,
    ml_bets,
    record,
    spread_bets,
    wilson,
)


def frames():
    pred = pd.DataFrame({"game_id": [1, 2, 3, 4], "point": "pretip", "season": 2023,
                         "margin": [10, -3, 5, 1], "total": 220, "pred": [8.0, 2.0, -1.0, 0.5],
                         "pred_total": 220.0, "sigma": 12.0})  # fmt: skip
    mkt = pd.DataFrame({"open_spread_home": [-3.0, -1.0, -4.0, np.nan],
                        "close_spread_home": [-4.0, -1.5, -6.0, -1.0],
                        "open_ml_home": [-150, -115, -180, np.nan],
                        "open_ml_away": [130, -105, 155, np.nan],
                        "close_ml_home": [-170, -120, -230, -115],
                        "close_ml_away": [150, 100, 190, -105],
                        "open_total": np.nan, "close_total": np.nan},
                       index=pd.Index([1, 2, 3, 4], name="game_id"))  # fmt: skip
    return pred, mkt


def test_spread_bets_at_open_and_close():
    pred, mkt = frames()
    b = spread_bets(pred, mkt, 3.0, "open").set_index("game_id")
    # game 1: 8 - 3 = +5 home, covers (10 > 3); game 3: -1 - 4 = -5 away, 5 - 4 = 1 > 0 home covers
    assert list(b.index) == [1, 3]
    assert b.at[1, "result"] == "win" and b.at[3, "result"] == "loss"
    assert b.at[1, "clv"] == 1.0  # took home -3, closed -4: a point better
    c = spread_bets(pred, mkt, 3.0, "close").set_index("game_id")
    assert 4 not in c.index and set(c.index) <= {1, 2, 3, 4}
    assert record(c)["bets"] == len(c)


def test_moneyline_bets():
    pred, mkt = frames()
    probs = np.array([0.80, 0.60, 0.30, 0.5])
    b = ml_bets(pred, mkt, 5.0, probs, "open").set_index("game_id")
    assert 1 in b.index and b.at[1, "result"] == "win"
    assert b.at[1, "profit"] == pytest.approx(100 / 150)
    assert 4 not in b.index  # no opening moneyline


def test_wilson_and_threshold_choice():
    lo, hi = wilson(55, 100)
    assert lo < 0.55 < hi and hi - lo == pytest.approx(0.19, abs=0.01)
    assert math.isnan(wilson(0, 0)[0])
    pred, mkt = frames()
    t, by = choose_threshold(lambda th: spread_bets(pred, mkt, th, "close"), (1.0, 3.0))
    assert t == 1.0 and set(by) == {1.0, 3.0}  # too few bets anywhere: the smallest


def test_blend_weight():
    pred, mkt = frames()
    w, by = blend_weight(pred, mkt)
    assert 0.0 <= w <= 1.0 and by[w] == min(by.values())
