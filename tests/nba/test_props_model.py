"""M15 NBA prop model core: the negative binomial over/push probabilities and a fit on a
synthetic panel (minutes from recent minutes, stats from minutes x rate)."""

import numpy as np
import pandas as pd
import pytest

from betdata.nba.props import model as M
from betdata.nba.props.panel import MARKETS


def test_dispersion_probabilities():
    d = M.Dispersion(a=2.0, b=1.5, c=0.01)
    mu = np.array([10.0, 10.0, 10.0])
    p = d.p_over(mu, np.array([5.5, 9.5, 14.5]))
    assert p[0] > p[1] > p[2] and 0 < p[2] < 0.5 < p[0] < 1
    # A whole-number line: P(over) + P(push) + P(under) = 1, and a half line has no push.
    over, push = d.p_over(mu[:1], np.array([10.0])), d.p_push(mu[:1], np.array([10.0]))
    assert 0 < push[0] < 0.2 and over[0] < d.p_over(mu[:1], np.array([9.5]))[0]
    assert d.p_push(mu[:1], np.array([9.5]))[0] == 0.0


def _panel(n=4000, seed=3):
    rnd = np.random.default_rng(seed)
    f = pd.DataFrame({"min_l10": rnd.uniform(10, 36, n)})
    for c in ("min_l3", "min_l30", "min_ewm"):
        f[c] = f.min_l10 + rnd.normal(0, 2, n)
    for c in ("start_l10", "b2b", "rest", "pf_rate", "own_q", "own_prob", "new_team",
              "mis_min", "mis_start_min", "side_margin"):
        f[c] = 0.0
    f["since_return"], f["n_team"], f["n_season"], f["abs_margin"] = 50, 50, 30, 5.0
    f["side_total"] = 220.0
    f["minutes"] = np.clip(f.min_l10 + rnd.normal(0, 3, n), 1, 48)
    for s in MARKETS.values():
        rate = {"pts": 0.45, "reb": 0.18, "ast": 0.1, "fg3m": 0.05, "pra": 0.73}[s]
        f[f"r10_{s}"] = f[f"r30_{s}"] = rate
        f[f"g10_{s}"] = rate * f.min_l10
        f[f"mis_{s}"] = 0.2
        f[f"opp_{s}"] = 1.0
        f[s] = rnd.poisson(rate * f.minutes)
    return M.prepare(f)


def test_fit_recovers_minutes_times_rate():
    f = _panel()
    mod = M.fit(f)
    m_hat = mod.predict_minutes(f)
    assert np.abs(m_hat - f.minutes).mean() < 3.0
    mu = mod.predict(f, "points", m_hat)
    assert np.corrcoef(mu, 0.45 * f.minutes)[0, 1] > 0.9
    back = M.PropModel.from_dict(mod.to_dict())
    assert back.predict(f.head(5), "assists") == pytest.approx(mod.predict(f.head(5), "assists"))
