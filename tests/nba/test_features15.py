"""M15 NBA side groups on the synthetic league: the as-of harness (verification b) passes for
every new group, and the groups produce finite values."""

import math

import pytest

from betdata.nba.asof import check
from betdata.nba.data import load_store
from betdata.nba.features15 import GROUPS15, fit_load
from betdata.nba.pipeline import game_features
from tests.nba.synth import synth


@pytest.fixture
def league(engine):
    ids = synth(engine)
    with engine.connect() as conn:
        store = load_store(conn)
    return store.with_params(load_sit={"000": 0.03, "100": 0.2}), ids


def test_asof_harness_every_m15_group(league):
    store, ids = league
    assert check(store, list(GROUPS15.values()), ids[20:28]) == []


def test_values_finite_and_load_table(league):
    store, ids = league
    x = game_features(store, list(GROUPS15.values()), ids[30], "pretip")
    assert set(x) >= {"m_luck3_net", "m_load_exp", "m_integ_new", "m_style_reb"}
    assert all(math.isfinite(v) for v in x.values())
    table = fit_load(store, (2021,))
    assert all(0 <= v <= 1 for k, v in table.items() if not k.startswith("n:"))
