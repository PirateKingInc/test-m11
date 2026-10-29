"""M18 Part G2: alternate rungs priced from the engine's simulated distributions (key numbers
included); alerts only when the engine agrees with Pinnacle's main number and a DK/FD rung is
>= 4 pp off in the same direction as Pinnacle's carry."""

import numpy as np
import pytest

from betdata.alerts import rungs as G
from betdata.alerts.feeds import Quote
from betdata.moneyline import no_vig


def margin_hist(values, weights):
    h = np.zeros(121)
    for v, w in zip(values, weights, strict=True):
        h[v + 60] += w
    return h.tolist()


def test_cover_prob_with_pushes_and_key_numbers():
    h = margin_hist([-7, -3, 0, 3, 7, 10], [10, 20, 5, 25, 25, 15])  # home margin
    # home -3: wins on 7, 10 (40), pushes on 3 (25): 40 / 75
    assert G.cover_prob(h, 60, "home", -3.0, "spreads") == pytest.approx(40 / 75)
    assert G.cover_prob(h, 60, "away", 3.0, "spreads") == pytest.approx(35 / 75)
    # -3.5 has no push: 40 / 100
    assert G.cover_prob(h, 60, "home", -3.5, "spreads") == pytest.approx(0.40)
    assert G.hist_median(h, 60) == 3.0


def test_judge_needs_agreement_and_direction():
    h = margin_hist([-3, 3, 7, 10], [25, 25, 25, 25])  # median 3: engine fair line -3
    alts = [Quote("nfl", 1, "sharpapi", "draftkings", "alt_spreads", "home", -6.5, 150),
            Quote("nfl", 1, "sharpapi", "draftkings", "alt_spreads", "away", 6.5, -180)]
    pairs = G.rung_pairs(alts)
    assert pairs == [(1, "draftkings", "spreads", -6.5, 150, -180)]
    eng = {1: {"margin_hist": h}}
    # home -6.5 covers on 7 and 10: 50% vs DK no-vig ~38%
    [r] = G.judge(pairs, eng, {(1, "spreads"): -3.0}, lambda *a: 0.45)
    assert (r.side, r.line, r.price) == ("home", -6.5, 150)
    assert r.edge == pytest.approx(0.5 - no_vig(150, -180)[0])
    assert G.judge(pairs, eng, {(1, "spreads"): -5.0}, lambda *a: 0.45) == []  # 2 pts apart
    assert G.judge(pairs, eng, {(1, "spreads"): -3.0}, lambda *a: 0.30) == []  # Pinnacle no
