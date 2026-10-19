import numpy as np
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
