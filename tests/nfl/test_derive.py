"""Derived tables: pre-game facts from the archived schedule, play-by-play aggregates."""

from datetime import timedelta

from sqlalchemy import select

from betdata.db.schema import (
    game_source_ids,
    games,
    nfl_game_info,
    nfl_player_game_pbp,
    nfl_team_game_pbp,
)
from betdata.nfl.derive import derive_game_info, derive_pbp


def game_id(conn, nflverse_id):
    return conn.execute(select(game_source_ids.c.game_id).where(
        game_source_ids.c.source_game_id == nflverse_id)).scalar()  # fmt: skip


def test_game_info_holds_only_pre_game_facts(engine, loaded):
    with engine.begin() as conn:
        assert derive_game_info(conn) == 8
        gid = game_id(conn, "1999_01_MIN_ATL")
        row = conn.execute(
            select(nfl_game_info).where(nfl_game_info.c.game_id == gid)
        ).mappings().one()
    assert (row["roof"], row["surface"], row["div_game"]) == ("dome", "astroturf", False)
    assert (row["home_coach"], row["away_coach"], row["referee"]) == (
        "Dan Reeves", "Dennis Green", "Gerry Austin")  # fmt: skip
    assert not {"temp", "wind", "home_qb_id", "result"} & set(row)


def test_pbp_aggregates(engine, loaded):
    with engine.begin() as conn:
        assert derive_pbp(conn, loaded) > 0
        gid = game_id(conn, "2025_01_DAL_PHI")
        kickoff = conn.execute(select(games.c.start_time_utc).where(games.c.game_id == gid)
                               ).scalar()  # fmt: skip
        teams = conn.execute(select(nfl_team_game_pbp).where(nfl_team_game_pbp.c.game_id == gid)
                             ).mappings().all()  # fmt: skip
        players = conn.execute(select(nfl_player_game_pbp).where(
            nfl_player_game_pbp.c.game_id == gid)).mappings().all()  # fmt: skip
        assert derive_pbp(conn, loaded) > 0  # rebuild replaces, never duplicates
        n = conn.execute(select(nfl_team_game_pbp.c.row_id)).all()
    assert len(n) == 4
    assert len(teams) == 2 and {t["known_at"] for t in teams} == {kickoff + timedelta(hours=12)}
    for t in teams:
        s = t["stats"]
        assert s["plays"] == s["pass_plays"] + s["rush_plays"] > 40
        assert 0 <= s["explosive"] <= s["plays"] and s["pass_att"] <= s["pass_plays"]
    qbs = [p for p in players if p["stats"]["dropbacks"] > 20]
    assert len(qbs) == 2  # one starting QB per side
    assert sum(p["stats"]["dropbacks"] for p in players) == sum(
        t["stats"]["pass_plays"] for t in teams)  # fmt: skip
