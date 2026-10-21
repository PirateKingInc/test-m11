"""M15 NFL side groups: the as-of harness, and every feature defined for every game."""

import numpy as np
import pytest

from betdata.nfl.asof import check
from betdata.nfl.features.availability import learn_play_prob
from betdata.nfl.groups import GROUPS
from betdata.nfl.groups15 import GROUPS15
from betdata.nfl.pipeline import game_features
from tests.nfl.synth15 import make_store15


@pytest.fixture(scope="module")
def store():
    s = make_store15()
    return s.with_params(**learn_play_prob(s, [2018]))


@pytest.mark.parametrize("group", list(GROUPS15))
def test_m15_group_passes_the_harness(store, group):
    ids = list(store.games.index)[7::11][:10]
    assert check(store, [GROUPS["base"], GROUPS15[group]], ids) == []


@pytest.mark.parametrize("group", list(GROUPS15))
def test_m15_group_is_defined_for_every_game(store, group):
    for gid in (int(store.games.index[0]), int(store.games.index[-1])):
        x = game_features(store, [GROUPS15[group]], gid, "prekick")
        assert x and all(np.isfinite(v) for v in x.values())
        assert all(k.startswith((f"m_{group}_", f"t_{group}_")) for k in x)


def test_first_game_has_no_history_and_no_margin_edge(store):
    gid = int(store.games.index[0])
    x = game_features(store, list(GROUPS15.values()), gid, "final_report")
    assert all(v == 0.0 for k, v in x.items() if k.startswith("m_"))
