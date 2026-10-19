"""Part B: every feature group rebuilt as of a prediction point is unchanged when later data
is removed or scrambled; and the harness really catches a leak."""

import pytest

from betdata.nfl.asof import check
from betdata.nfl.groups import GROUPS
from betdata.nfl.store import AsOf
from tests.nfl.synth import make_store


@pytest.fixture(scope="module")
def store():
    return make_store()


def sample_games(store, n=12):
    ids = list(store.games.index)
    step = max(1, len(ids) // n)
    return ids[step::step][:n]


@pytest.mark.parametrize("group", list(GROUPS))
def test_group_passes_the_harness(store, group):
    groups = [GROUPS["base"], GROUPS[group]] if group != "base" else [GROUPS["base"]]
    assert check(store, groups, sample_games(store)) == []


class PeeksAtTheFuture:
    """Reads the final score of the game being predicted, ignoring known_at."""

    name = "leak"

    def compute(self, a: AsOf, g, point):
        r = a.store.frame("results").df.set_index("game_id")["home_score"]
        return {"m_leak_score": float(r.get(g.game_id, float("nan")))}


def test_harness_catches_a_leak(store):
    bad = check(store, [PeeksAtTheFuture()], sample_games(store, 3))
    assert bad and all(v.features == ("m_leak_score",) for v in bad)
