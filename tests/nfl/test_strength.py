"""Group 2: opponent-adjusted ratings and preseason priors."""

import numpy as np
import pandas as pd

from betdata.nfl.features.strength import _ratings, _team_rates


def league(n_weeks=12, seed=3):
    """Team 1 passes much better than everyone; team 2's defense allows a lot."""
    rng = np.random.default_rng(seed)
    rows, gid = [], 0
    start = pd.Timestamp("2024-09-08", tz="UTC")
    teams = list(range(1, 9))
    for week in range(n_weeks):
        order = rng.permutation(teams)
        for i in range(0, 8, 2):
            gid += 1
            for off, dfn, home in ((order[i], order[i + 1], True), (order[i + 1], order[i], False)):
                epa = 0.0 + 0.3 * (off == 1) + 0.2 * (dfn == 2) + rng.normal(0, 0.05)
                rows.append(dict(game_id=gid, season=2024, team=int(off), opp=int(dfn), home=home,
                                 neutral=False, kickoff=start + pd.Timedelta(days=7 * week),
                                 pass_epa=epa * 35, pass_plays=35, rush_epa=-0.05 * 25,
                                 rush_plays=25, pass_succ=16, rush_succ=10, plays=60,
                                 early_epa=1.0, early_plays=40, explosive=6, ints=1,
                                 pass_att=33, game_fumbles=2, fumble_recoveries=1,
                                 neutral_secs=800, neutral_secs_n=30, neutral_pass=18,
                                 neutral_plays=30))  # fmt: skip
    return pd.DataFrame(rows)


def test_ratings_separate_offense_from_the_defenses_it_faced():
    r = _ratings(league())["pass"]
    assert max(r["off"], key=r["off"].get) == 1
    assert max(r["def"], key=r["def"].get) == 2  # defense rating = what it allows
    # shrunk toward average (250-play prior) but clearly separated from everyone else
    assert r["off"][1] > 0.1 > max(v for t, v in r["off"].items() if t != 1)
    assert r["def"][2] > 0.05 > max(v for t, v in r["def"].items() if t != 2)


def test_empty_history_gives_neutral_ratings():
    assert _ratings(pd.DataFrame())["pass"]["off"] == {}


def test_team_rates_luck_is_zero_for_even_recoveries():
    rates = _team_rates(league())
    assert all(abs(v["fum_luck"]) < 1e-9 for v in rates.values())
    assert all(abs(v["secs"] - 800 / 30) < 1e-9 for v in rates.values())
