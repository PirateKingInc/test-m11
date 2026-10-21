"""M14 NBA store: prediction points, player-name matching, and the loader's as-of rules on a
synthetic league written through the real tables."""

from datetime import UTC, datetime, timedelta

import pytest

from betdata.nba.data import load_store, norm_name
from betdata.nba.points import morning_time, point_time, pretip_time
from tests.nba.synth import ET, synth


def test_points():
    tip = datetime(2026, 10, 21, 23, 30, tzinfo=UTC)  # 7:30 PM EDT
    assert pretip_time(tip).astimezone(ET).hour == 19 and pretip_time(tip) < tip
    assert morning_time(tip).astimezone(ET).hour == 14
    late = datetime(2026, 10, 22, 2, 30, tzinfo=UTC)  # 10:30 PM EDT
    assert pretip_time(late).astimezone(ET).hour == 22
    on_hour = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)  # 8:00 PM sharp: the 7 PM run
    assert pretip_time(on_hour).astimezone(ET).hour == 19
    noon = datetime(2026, 12, 25, 17, 0, tzinfo=UTC)  # 12:00 PM EST
    assert morning_time(noon).astimezone(ET).hour == 9 and pretip_time(noon).astimezone(
        ET).hour == 11
    assert point_time("morning", tip) == morning_time(tip)


def test_names():
    assert norm_name("Porter Jr., Michael") == norm_name("Michael Porter Jr.") == "michael porter"
    assert norm_name("Dončić, Luka") == "luka doncic"


@pytest.fixture
def league(engine):
    ids = synth(engine)
    with engine.connect() as conn:
        store = load_store(conn)
    return store, ids


def test_store_rules(league):
    store, ids = league
    box = store.frame("box").df
    g = store.games.loc[ids[5]]
    assert box.set_index("game_id").at[ids[5], "known_at"] == g.tip + timedelta(hours=3)
    odds = store.frame("odds").df
    opening = odds[(odds.game_id == ids[5]) & (odds.kind == "open")].iloc[0]
    # Backfilled after the game: known at 07:00 ET on game day.
    assert opening.known_at.tz_convert(ET).hour == 7
    inj = store.frame("injuries").df
    assert inj.player_id.notna().all()  # "T0, P0" matched to the box score's "P0 T0"
