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


def test_injury_names_match_players_who_changed_teams(engine):
    """A player seen with two teams in box scores is still one player for the name map."""
    from sqlalchemy import insert, select

    from betdata.core.matching import sport_id
    from betdata.db.schema import nba_injury_reports, nba_player_games
    from betdata.db.schema import teams as teams_t

    ids = synth(engine, n_days=4)
    with engine.begin() as conn:
        sid = sport_id(conn, "nba")
        tid = dict(conn.execute(select(teams_t.c.canonical_name, teams_t.c.team_id)
                                .where(teams_t.c.sport_id == sid)).all())  # fmt: skip
        rows = conn.execute(select(nba_player_games).where(
            nba_player_games.c.player_id == "00")).mappings().all()  # "P0 T0"
        other = next(g for g in ids if g not in {r["game_id"] for r in rows})
        conn.execute(insert(nba_player_games).values(
            {**{k: v for k, v in rows[0].items() if k != "player_game_id"}, "game_id": other,
             "team_id": tid["Utah Jazz"]}))  # the same player, now with another team
        conn.execute(insert(nba_injury_reports).values(
            game_date="2021-10-21", matchup="X@Y", game_id=ids[1], team_id=tid["Utah Jazz"],
            team_name="Utah Jazz", player_name="Tma, Pla", status="Out", reason=None,
            report_at=datetime(2021, 10, 21, 15, tzinfo=UTC),
            known_at=datetime(2021, 10, 21, 15, tzinfo=UTC), source="nba_injury",
            fetched_at=datetime(2021, 10, 21, 15, tzinfo=UTC)))  # fmt: skip
        store = load_store(conn)
    inj = store.frame("injuries").df
    assert set(inj[inj.player_name == "Tma, Pla"].player_id) == {"00"}
