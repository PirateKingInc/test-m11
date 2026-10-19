import math

import numpy as np
import pandas as pd
import pytest

from betdata.moneyline import (
    Calibration,
    MoneylineConfig,
    clv_pts,
    edge_pts,
    fair_american,
    fit_platt,
    implied,
    no_vig,
    profit,
)
from betdata.nfl import moneyline as nml


def test_fair_odds_round_trip():
    assert fair_american(0.6) == -150
    assert fair_american(0.4) == 150
    assert fair_american(0.5) == -100
    assert fair_american(0.0) is None and fair_american(1.0) is None
    for p in (0.2, 0.35, 0.5, 0.71, 0.9):
        assert implied(fair_american(p)) == pytest.approx(p, abs=0.002)


def test_no_vig_and_edge():
    assert implied(-110) == pytest.approx(0.5238, abs=1e-4)
    h, a = no_vig(-110, -110)
    assert (h, a) == (pytest.approx(0.5), pytest.approx(0.5))
    h, a = no_vig(-200, 170)
    assert h + a == pytest.approx(1.0)
    assert h == pytest.approx((2 / 3) / (2 / 3 + 100 / 270))
    assert no_vig(None, 120) is None
    assert edge_pts(0.58, 0.53) == 5.0


def test_profit_and_clv():
    assert profit("win", 150) == 1.5
    assert profit("win", -200) == 0.5
    assert profit("loss", 150) == -1.0
    assert profit("push", -110) == 0.0 and profit("void", 120) == 0.0
    assert clv_pts(0.40, 0.43) == 3.0  # the market moved toward the backed side
    assert clv_pts(0.40, None) is None


def test_platt_recovers_known_slope():
    rng = np.random.default_rng(0)
    p = rng.uniform(0.1, 0.9, 20000)
    z = 0.6 * np.log(p / (1 - p)) + 0.1  # outcomes from a shrunk version of p
    y = rng.random(len(p)) < 1 / (1 + np.exp(-z))
    slope, intercept = fit_platt(p, y)
    assert slope == pytest.approx(0.6, abs=0.05)
    assert intercept == pytest.approx(0.1, abs=0.05)
    cal = Calibration(slope, intercept, applied=True)
    assert cal(0.8) < 0.8  # overconfident model pulled toward 50%
    assert Calibration(slope, intercept, applied=False)(0.8) == 0.8


def test_config_round_trip(tmp_path):
    cfg = MoneylineConfig(Calibration(0.9, 0.02, "test", True), 4.0, "test")
    cfg.save(tmp_path / "ml.json")
    back = MoneylineConfig.load(tmp_path / "ml.json")
    assert back.threshold_pts == 4.0 and back.calibration.applied
    assert back.calibration(0.7) == pytest.approx(cfg.calibration(0.7))


def _frame():
    pred = pd.DataFrame({
        "game_id": [1, 2, 3, 4], "point": "prekick", "margin": 0.0, "total": 0.0,
        "win_prob": [0.70, 0.40, 0.52, 0.60],
    })  # fmt: skip
    out = pd.DataFrame({"margin": [7, -3, 3, 0], "total": [40] * 4}, index=[1, 2, 3, 4])
    closes = {1: (-150, 130), 2: (-120, 100), 3: (-110, -110), 4: (-110, -110)}
    return nml.frame(pred, out, closes)


def test_moneyline_bets_against_no_vig_close():
    d = _frame()
    assert math.isnan(d.home_won.iloc[3])  # a tie
    bl = nml.bets(d, 5.0, Calibration())
    by = {b["game_id"]: b for b in bl}
    # Game 1: model 0.70 vs no-vig ~0.58 -> back home at -150, won.
    assert by[1]["side"] == "home" and by[1]["result"] == "win"
    assert by[1]["profit"] == pytest.approx(100 / 150)
    # Game 2: model 0.40 vs ~0.52 -> back away at +100, home lost so away won.
    assert by[2]["side"] == "away" and by[2]["result"] == "win" and by[2]["profit"] == 1.0
    assert 3 not in by  # 0.52 vs 0.50: below the threshold
    assert by[4]["result"] == "push"  # tie
    s = nml.summary(bl)
    assert s["record"] == "2-0-1" and s["bets"] == 3


def test_scores_compare_model_and_market():
    res = nml.scores(_frame(), Calibration())
    assert res["games"] == 3  # the tie is dropped
    assert res["brier_market"] > 0 and res["brier_model"] > 0
    assert sum(b[2] for b in res["bins_model"]) == 3
