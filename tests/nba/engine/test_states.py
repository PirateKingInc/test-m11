"""M20 B5/B7 building blocks: recency sums and the league's as-of level."""

import numpy as np

from betdata.nba.engine import team as T
from betdata.nba.engine.states import ew
from tests.nba.engine.league import frames


def test_ew_restarts_at_group_starts():
    v = np.array([1.0, 1.0, 1.0, 5.0, 5.0])
    s = ew(v, np.array([True, False, False, True, False]), h=1)
    assert np.allclose(s, [1, 1.5, 1.75, 5, 7.5])


def test_team_states_start_each_season_from_the_prior(pads):
    _, _, tg, _ = frames()
    ts = T.team_states(tg, pads)
    first = ts[ts.season_prior]
    assert len(first) == 4 and (first.game_id == -1).all()
    # a team's first game reads the prior (known before the season), not its own result
    a = T.asof(ts, [1], [tg.tip.min()], [2021])
    assert np.isclose(a.pace.iloc[0], first[first.team_id == 1].pace.iloc[0])


def test_league_level_is_as_of():
    _, _, tg, _ = frames()
    ls = T.league_states(tg)
    early = T.league_asof(ls, [tg.known_at.min()], {})
    late = T.league_asof(ls, [tg.known_at.max()], {})
    assert early.columns.tolist() == late.columns.tolist()
    assert np.isfinite(late.use.iloc[0]) and 0.5 < late.credit.iloc[0] <= 1.2
