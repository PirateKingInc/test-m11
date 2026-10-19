"""The as-of store loaded from the database: every frame carries its known_at rule."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select

from betdata.core.matching import sport_id
from betdata.db.schema import game_source_ids, injuries, roster_weeks
from betdata.nfl.asof import check
from betdata.nfl.data import load_store
from betdata.nfl.derive import derive_game_info, derive_pbp
from betdata.nfl.groups import GROUPS
from betdata.nfl.points import final_report_time, prekick_time


def test_store_known_at_rules(engine, loaded):
    with engine.begin() as conn:
        derive_game_info(conn)
        derive_pbp(conn, loaded)
        gid = conn.execute(select(game_source_ids.c.game_id).where(
            game_source_ids.c.source_game_id == "2025_01_DAL_PHI")).scalar()  # fmt: skip
        sid = sport_id(conn, "nfl")
        store0 = load_store(conn)
        g = store0.games.loc[gid]
        conn.execute(insert(injuries).values(
            sport_id=sid, team_id=int(g.home), season=2025, week=1, player_name="A",
            source_player_id="00-1", report_status="Out", source="nflverse",
            fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
            report_date=g.kickoff - timedelta(days=2)))  # fmt: skip
        conn.execute(insert(roster_weeks).values(
            source="nflverse", season=2025, week=1, team_id=int(g.home), gsis_id="00-1",
            player_name="A", status="INA"))  # fmt: skip
        store = load_store(conn)
    g = store.games.loc[gid]
    assert g.roof_type == "outdoors" and g.home_rest == 14.0  # first game of the season
    res = store.frame("results").df.set_index("game_id")
    assert res.at[gid, "known_at"] == g.kickoff + timedelta(hours=12)
    tg = store.frame("team_games").df
    assert set(tg[tg.game_id == gid].known_at) == {g.kickoff + timedelta(hours=12)}
    inj = store.frame("injuries").df
    assert inj.known_at.tolist() == [g.kickoff - timedelta(days=2)]  # publish time
    ros = store.frame("rosters").df
    assert ros.known_at.tolist() == [prekick_time(g.kickoff)]  # inactive: game-day news
    coaches = store.frame("coaches").df.set_index("game_id")
    assert coaches.at[gid, "known_at"] == final_report_time(g.kickoff)
    assert store.frame("odds").df.known_at.max() <= datetime(2026, 10, 16, tzinfo=UTC)
    assert check(store, [GROUPS["base"]], [gid]) == []
