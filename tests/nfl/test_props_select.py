"""M15 NFL prop selection rules: the miscalibration rule and the Platt fit."""

import numpy as np

from betdata.nfl.props.select import miscalibrated, platt


def test_platt_and_miscalibration_rule():
    rng = np.random.default_rng(0)
    p = rng.uniform(0.1, 0.9, 20000)
    y = (rng.uniform(size=p.size) < np.clip(p + 0.1, 0, 1)).astype(float)  # under-predicted
    a, b = platt(p, y)
    assert b > 0 and 0.5 < a < 2
    assert miscalibrated([{"n": 1000, "pred": 0.40, "actual": 0.45}])
    assert not miscalibrated([{"n": 1000, "pred": 0.40, "actual": 0.41}])
    assert not miscalibrated([{"n": 100, "pred": 0.40, "actual": 0.45},
                              {"n": 5000, "pred": 0.5, "actual": 0.5}])
