"""Group 1: who is expected to play, and what losing him is worth."""

from datetime import timedelta

import pandas as pd
import pytest

from betdata.nfl.features.availability import (
    Availability,
    learn_play_prob,
    status_key,
    team_features,
)
from betdata.nfl.points import final_report_time, point_time
from betdata.nfl.store import AsOf, Frame, Store
from tests.nfl.synth import make_store


@pytest.mark.parametrize(
    "args, key",
    [
        (("prekick", "ACT", True, "ACT", True, ("Questionable", None, "WR")),
         "pk_act|Questionable"),
        (("prekick", "INA", True, "ACT", True, None), "pk_out"),
        (("prekick", None, True, "ACT", True, None), "pk_gone"),
        (("final_report", None, False, "ACT", True,
          ("Out", "Did Not Participate In Practice", "QB")), "fr|Out|DNP"),
        (("final_report", None, False, "RES", True, None), "fr_reserve"),
        (("final_report", None, False, None, False, None), "fr_gone"),
        (("final_report", None, False, "ACT", True, None), "fr|-|-"),
        # prekick without a game-day roster (older seasons) falls back to the report
        (("prekick", None, False, "ACT", True, ("Doubtful", None, "CB")), "fr|Doubtful|-"),
    ],
)  # fmt: skip
def test_status_key(args, key):
    assert status_key(*args) == key


@pytest.fixture(scope="module")
def store():
    """Synthetic league where players reported Out really sit out."""
    s = make_store()
    inj = s.frame("injuries").df
    out = {(r.season, r.week, r.team, r.key) for r in inj[inj.report_status == "Out"]
           .itertuples(index=False)}  # fmt: skip
    snaps = s.frame("snaps").df
    keep = [(r.season, r.week, r.team, r.key) not in out for r in snaps.itertuples(index=False)]
    s = Store(s.games, {**s.frames, "snaps": Frame(snaps[keep])})
    return s.with_params(**learn_play_prob(s, [2018]))


def with_report(store: Store, rows: list[dict]) -> Store:
    inj = pd.concat([store.frame("injuries").df, pd.DataFrame(rows)], ignore_index=True)
    return Store(store.games, {**store.frames, "injuries": Frame(inj)}, store.params)


def test_starting_qb_out_lowers_expected_qb_value(store):
    g = store.games
    game = g[(g.season == 2019) & (g.week == 6)].iloc[0]
    t = final_report_time(game.kickoff)
    snaps = store.frame("snaps").df
    qb = (snaps[(snaps.team == game.home) & (snaps.group == "QB") & (snaps.kickoff < t)]
          .key.value_counts().index[0])  # fmt: skip
    before = team_features(AsOf(store, t), game, game.home, "final_report")
    out = with_report(store, [dict(season=game.season, week=game.week, team=game.home, key=qb,
                                   group="QB", injury_id=10**6, report_status="Out",
                                   practice_status="Did Not Participate In Practice",
                                   known_at=t - timedelta(hours=1))])  # fmt: skip
    after = team_features(AsOf(out, t), game, game.home, "final_report")
    assert after["qb_value"] != before["qb_value"]
    assert after["qb_backup_inexp"] > before["qb_backup_inexp"]


def test_a_report_published_after_the_point_is_not_seen(store):
    g = store.games
    game = g[(g.season == 2019) & (g.week == 6)].iloc[0]
    t = point_time("final_report", game.kickoff)
    late = with_report(store, [dict(season=game.season, week=game.week, team=game.home,
                                    key=f"P{game.home}-OL0", group="OL", injury_id=10**6,
                                    report_status="Out", practice_status=None,
                                    known_at=t + timedelta(minutes=1))])  # fmt: skip
    a, b = Availability().compute(AsOf(store, t), game, "final_report"), Availability().compute(
        AsOf(late, t), game, "final_report")  # fmt: skip
    assert a == b


def test_learned_play_probabilities_are_ordered(store):
    p = store.params["play_prob"]
    outs = [v for k, v in p.items() if k.startswith("fr|Out")]
    assert outs and max(outs) < 0.6 < p["fr|-|-"]
    assert store.params["qb_repl"] < 0.1
