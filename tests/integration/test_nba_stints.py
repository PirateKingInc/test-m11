"""M15 lineup stints stored in `nba_stints`: from an ESPN game summary (live; hand-made fixture)
and from a sportsdataverse play-by-play season file (history; built in the test)."""

import io
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import delete, func, insert, select

from betdata.core.matching import sport_id
from betdata.db.schema import game_source_ids, games, nba_player_games, nba_stints, teams
from betdata.sources.espn_nba import EspnNbaSource
from betdata.sources.sportsdataverse import SportsDataverseSource
from tests.conftest import fixture_bytes

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


def _box(engine, gid):
    with engine.connect() as conn:
        return conn.execute(select(nba_player_games).where(nba_player_games.c.game_id == gid)
                            .order_by(nba_player_games.c.player_id)).mappings().all()  # fmt: skip


@pytest.fixture
def espn(engine, settings, fake_api, clock):
    return EspnNbaSource(engine, settings.source("espn_nba"), transport=fake_api.transport,
                         sleep=lambda s: None, clock=clock)  # fmt: skip


def test_summary_box_score_and_stints(espn, engine, fake_api, clock):
    clock.now = TIP + timedelta(hours=3)
    gid, _, _ = _game(engine, "401585601")
    fake_api.add(body=fixture_bytes("espn_nba", "summary_stints.json"))
    res = espn.run("boxscores", days=2)
    assert res.status == "ok", res.message
    assert res.rows_inserted == 12  # box rows, as before
    box = _box(engine, gid)
    assert sum(r["starter"] for r in box) == 10
    s = _stints(engine, gid)
    assert [(r["period"], r["start_sec"], r["end_sec"]) for r in s] == [
        (1, 0, 360), (1, 360, 720), (2, 720, 790), (2, 790, 1440)]
    assert s[0]["home_players"] == "101,102,103,104,105"
    assert s[1]["home_players"] == "102,103,104,105,106"
    assert s[2]["away_players"] == "201,202,203,204,205"  # 202 filled from period 1
    assert s[3]["away_players"] == "201,202,204,205,206"
    assert [(r["home_pts"], r["away_pts"]) for r in s] == [(2, 3), (2, 0), (4, 2), (2, 0)]
    assert s[2]["home_poss"] == pytest.approx(1.88) and s[0]["away_poss"] == 1
    assert all(r["known_at"] == clock.now and r["source"] == "espn_nba" for r in s)

    # Games fetched before stints existed: the `stints` job fetches the summary again and
    # stores stints only; the stored box score is not touched.
    with engine.begin() as conn:
        conn.execute(delete(nba_stints))
    clock.now += timedelta(hours=1)
    fake_api.add(body=fixture_bytes("espn_nba", "summary_stints.json"))
    res = espn.run("stints", days=2)
    assert res.status == "ok" and res.rows_inserted == 4, res.message
    assert _box(engine, gid) == box
    assert len(_stints(engine, gid)) == 4
    before = len(fake_api.requests)
    espn.run("stints", days=2)  # nothing left: no request
    assert len(fake_api.requests) == before


def test_summary_without_plays_still_loads_box(espn, engine, fake_api, clock):
    clock.now = TIP + timedelta(hours=3)
    gid, _, _ = _game(engine, "401585601")
    fake_api.add(body=fixture_bytes("espn_nba", "summary_401585601.json"))  # no plays
    assert espn.run("boxscores", days=2).status == "ok"
    assert len(_box(engine, gid)) >= 20 and not _stints(engine, gid)


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
