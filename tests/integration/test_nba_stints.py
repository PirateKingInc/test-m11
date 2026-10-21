"""M15 lineup stints stored in `nba_stints`, from a sportsdataverse play-by-play season file
(history; built in the test)."""

import io
from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import func, insert, select

from betdata.core.matching import sport_id
from betdata.db.schema import game_source_ids, games, nba_player_games, nba_stints, teams
from betdata.sources.sportsdataverse import SportsDataverseSource

TIP = datetime(2024, 3, 17, 0, 30, tzinfo=UTC)


def _game(engine, espn_id, home="Los Angeles Lakers", away="Golden State Warriors"):
    with engine.begin() as conn:
        sid = sport_id(conn, "nba")
        tid = dict(conn.execute(select(teams.c.canonical_name, teams.c.team_id)
                                .where(teams.c.sport_id == sid)).all())  # fmt: skip
        gid = conn.execute(insert(games).values(
            sport_id=sid, season=2023, start_time_utc=TIP, home_team_id=tid[home],
            away_team_id=tid[away], status="final")).inserted_primary_key[0]  # fmt: skip
        conn.execute(insert(game_source_ids).values(source="espn", source_game_id=espn_id,
                                                    game_id=gid))  # fmt: skip
    return gid, tid[home], tid[away]


def _stints(engine, gid):
    with engine.connect() as conn:
        return conn.execute(select(nba_stints).where(nba_stints.c.game_id == gid)
                            .order_by(nba_stints.c.start_sec)).mappings().all()  # fmt: skip


def _pbp_file(espn_id: str) -> bytes:
    """A one-game season file in sportsdataverse's layout (ESPN ids as floats, as in 2025-26)."""
    rows, hs, as_ = [], 0, 0

    def add(period, clock, kind, team, a1=None, a2=None, pts=0, shot=False):
        nonlocal hs, as_
        hs, as_ = hs + (pts if team == 13 else 0), as_ + (pts if team == 9 else 0)
        rows.append(dict(game_id=int(espn_id), sequence_number=len(rows) + 1, type_text=kind,
                         team_id=team, athlete_id_1=a1, athlete_id_2=a2, athlete_id_3=None,
                         period_number=period, clock_display_value=clock, home_score=hs,
                         away_score=as_, shooting_play=shot, home_team_id=13))  # fmt: skip

    add(1, "11:40", "Jump Shot", 13, 101.0, pts=2, shot=True)
    add(1, "8:00", "Substitution", 9, 206.0, 201.0)
    add(1, "7:00", "Jump Shot", 9, 206.0, pts=3, shot=True)
    add(1, "0:0.0", "End Period", None)
    buf = io.BytesIO()
    pd.DataFrame(rows).astype({"team_id": "float64"}).to_parquet(buf)
    return buf.getvalue()


def test_season_file_stints_round_trip(engine, settings, fake_api, clock):
    sdv = SportsDataverseSource(engine, settings.source("sportsdataverse"),
                                transport=fake_api.transport, sleep=lambda s: None,
                                clock=clock)  # fmt: skip
    gid, home, away = _game(engine, "401585601")
    with engine.begin() as conn:
        conn.execute(insert(nba_player_games), [
            dict(game_id=gid, team_id=home if p < 200 else away, player_id=str(p),
                 player_name=f"P{p}", starter=p % 100 <= 5, did_not_play=False,
                 source="sportsdataverse", known_at=TIP, fetched_at=TIP)
            for p in [*range(101, 107), *range(201, 207)]])  # fmt: skip
    body = _pbp_file("401585601")
    for _ in range(2):  # loading the season twice replaces, never duplicates
        fake_api.add(body=body)
        res = sdv.run("pbp", seasons="2023")
        assert res.status == "ok", res.message
        assert fake_api.requests[-1].url.path.endswith("/espn_nba_pbp/play_by_play_2024.parquet")
    s = _stints(engine, gid)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(nba_stints)).scalar() == 2
    assert [(r["start_sec"], r["end_sec"], r["home_pts"], r["away_pts"]) for r in s] == [
        (0, 240, 2, 0), (240, 720, 0, 3)]
    assert s[1]["away_players"] == "202,203,204,205,206"
    assert all(r["known_at"] == TIP + timedelta(hours=3) for r in s)  # history: tip + 3 h
