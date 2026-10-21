"""M14 NBA features on a synthetic league: the as-of harness on every feature group (b), that
changing any result after a prediction point never changes that prediction, injury statuses
moving availability, and absences not carrying into a new season."""

from datetime import timedelta

import pytest
from sqlalchemy import update

from betdata.db.schema import nba_player_games, results
from betdata.nba.asof import check
from betdata.nba.data import load_store
from betdata.nba.features import GROUPS, availability
from betdata.nba.pipeline import game_features
from betdata.nfl.store import AsOf
from tests.nba.synth import synth


@pytest.fixture
def league(engine):
    ids = synth(engine)
    with engine.connect() as conn:
        store = load_store(conn)
    return store, ids


def test_asof_harness_every_group(league):
    store, ids = league
    groups = list(GROUPS.values())
    assert check(store, groups, ids[25:31]) == []


def test_later_results_never_change_a_prediction(league, engine):
    store, ids = league
    groups = list(GROUPS.values())
    gid = ids[20]
    before = {p: game_features(store, groups, gid, p) for p in ("morning", "pretip")}
    with engine.begin() as conn:  # rewrite every result from this game on
        for later in ids[20:]:
            conn.execute(update(results).where(results.c.game_id == later)
                         .values(home_score=150, away_score=60))  # fmt: skip
            conn.execute(update(nba_player_games).where(nba_player_games.c.game_id == later)
                         .values(minutes=1, plus_minus=40))  # fmt: skip
    with engine.connect() as conn:
        changed = load_store(conn)
    after = {p: game_features(changed, groups, gid, p) for p in ("morning", "pretip")}
    assert before == after


def test_injury_status_moves_availability(league):
    store, ids = league
    gid = ids[30]
    g = store.games.loc[gid]
    a_pre = AsOf(store, g.tip - timedelta(hours=8, minutes=1))  # before the report
    a_post = AsOf(store, g.tip - timedelta(hours=1))
    before = availability(a_pre, g, g.home)
    after = availability(a_post, g, g.home)
    rep = store.frame("injuries").df.set_index("game_id").loc[gid]
    if rep.status == "Out":
        assert after["new"] + after["long"] >= before["new"] + before["long"]
    assert after != before or rep.status == "Probable"


def test_absences_do_not_carry_into_a_new_season():
    from types import SimpleNamespace

    from betdata.nba.features import streak_for
    from betdata.nba.state import League, Player

    st = League()
    st.season = 2025
    p = Player(team=1, streak=15)  # sat out the end of last season
    assert streak_for(st, SimpleNamespace(season=2026), p) == 0  # opening night
    assert streak_for(st, SimpleNamespace(season=2025), p) == 15

