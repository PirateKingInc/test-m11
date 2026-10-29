"""M18 Part D (verification: median test): prop outputs carry the distribution's median and
quantiles; the median-anchored mean reproduces a target median; the median regression finds
the conditional median of a skewed stat; P(over) always comes from the distribution."""

import numpy as np
import pytest
from scipy.stats import nbinom

from betdata.nba.props.median import fit_median
from betdata.nba.props.model import Dispersion
from betdata.props.dist import mean_for_median, nb_params, quantiles, smooth_median

DISP = Dispersion(0.5, 1.2, 0.05)  # over-dispersed, like rebounds


def test_quantiles_are_the_distributions():
    q = quantiles(DISP, 7.3)
    n, p = nb_params(DISP, 7.3)
    assert q["median"] == nbinom.ppf(0.5, float(n), float(p))
    assert q["q10"] <= q["q25"] <= q["median"] <= q["q75"] <= q["q90"]
    # skewed right: the median sits below the mean
    assert q["median"] < 7.3
    assert quantiles(None, 0.4)["median"] == 0.0  # Poisson (anytime TD)


def test_mean_for_median_round_trip():
    target = np.array([2.0, 4.5, 6.0, 9.25, 15.0])
    mu = mean_for_median(DISP, target)
    assert smooth_median(DISP, mu) == pytest.approx(target, abs=1e-4)
    assert np.all(mu > target)  # right skew: mean above median
    # the over probability then comes from that distribution, not from the mean
    assert DISP.p_over(mu, target).tolist() == pytest.approx(
        (1 - nbinom.cdf(np.floor(target), *nb_params(DISP, mu))).tolist())


def test_median_regression_finds_the_conditional_median():
    rng = np.random.default_rng(3)
    x = rng.uniform(0, 10, 4000)
    y = 1.0 + 0.8 * x + rng.exponential(3.0, 4000)  # skewed noise: median of noise 3 ln 2
    r = fit_median(x[:, None], y, ("x",), lam=0.0)
    xs = np.array([[2.0], [8.0]])
    want = 1.0 + 0.8 * xs[:, 0] + 3.0 * np.log(2)
    assert r.predict(xs) == pytest.approx(want, abs=0.25)
    # least squares would land on the mean instead (3 above the line), clearly higher
    assert np.all(r.predict(xs) < 1.0 + 0.8 * xs[:, 0] + 2.5)


def test_page_shows_the_median():
    from datetime import UTC, datetime

    from betdata.page.build import props_html
    from betdata.sports import PropRow

    r = PropRow(1, datetime(2026, 11, 1, tzinfo=UTC), "BOS", "NYK", "Jalen Brunson", "assists",
                7.36, 6.5, -110, -110, 0.55, 5.0, median=7.0)  # fmt: skip
    html = props_html([r], {})
    assert "Median <b>7</b> (mean 7.4)" in html and "Proj <b>" not in html
