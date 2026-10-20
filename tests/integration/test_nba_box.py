"""M14 NBA player box scores from the sportsdataverse season files (saved real sample), and the
NBA season label of the 2020 bubble games."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, insert, select

from betdata.core.matching import sport_id
from betdata.db.schema import game_source_ids, games, nba_player_games, teams
from betdata.sources.sportsdataverse import REQUIRED, SportsDataverseSource, parse_schedule
from tests.conftest import fixture_bytes


@pytest.fixture
def sdv(engine, settings, fake_api, clock):
    return SportsDataverseSource(engine, settings.source("sportsdataverse"),
                                 transport=fake_api.transport, sleep=lambda s: None,
                                 clock=clock)  # fmt: skip


def count(engine, table, *where):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table).where(*where)).scalar()


def game(engine, espn_id, home, away, tip):
    with engine.begin() as conn:
        sid = sport_id(conn, "nba")
        tid = dict(conn.execute(select(teams.c.canonical_name, teams.c.team_id)
                                .where(teams.c.sport_id == sid)).all())  # fmt: skip
        gid = conn.execute(insert(games).values(
            sport_id=sid, season=2015, start_time_utc=tip, home_team_id=tid[home],
            away_team_id=tid[away], status="final")).inserted_primary_key[0]  # fmt: skip
        conn.execute(insert(game_source_ids).values(source="espn", source_game_id=espn_id,
                                                    game_id=gid))  # fmt: skip
    return gid


def test_season_box_scores(sdv, engine, fake_api):
    tip = datetime(2016, 1, 6, 1, 0, tzinfo=UTC)
    gid = game(engine, "400828408", "Atlanta Hawks", "New York Knicks", tip)
    game(engine, "400828409", "Chicago Bulls", "Milwaukee Bucks", tip)
    fake_api.add(body=fixture_bytes("sportsdataverse", "player_box_2016_sample.parquet"))
    res = sdv.run("player_box", seasons="2015")
    assert res.status == "ok", res.message
    assert fake_api.requests[-1].url.path.endswith("/player_box_2016.parquet")
    assert res.rows_inserted == 52
    with engine.connect() as conn:
        rows = conn.execute(select(nba_player_games).where(
            nba_player_games.c.game_id == gid)).mappings().all()  # fmt: skip
    assert len({r["team_id"] for r in rows}) == 2
    played = [r for r in rows if not r["did_not_play"]]
    assert all(r["minutes"] is not None for r in played)
    assert abs(sum(r["minutes"] for r in played) - 480) <= 30  # 240 per team (+ overtime)
    assert all((r["known_at"] - tip).total_seconds() == 3 * 3600 for r in rows)
    # Loading the same file again replaces, it does not duplicate.
    fake_api.add(body=fixture_bytes("sportsdataverse", "player_box_2016_sample.parquet"))
    sdv.run("player_box", seasons="2015")
    assert count(engine, nba_player_games) == 52


def test_bubble_games_keep_their_season():
    cols = sorted(REQUIRED | {"season", "season_type"})
    row = dict(id="401225000", start_date="2020-08-05T19:00Z", status_type_name="STATUS_FINAL",
               home_display_name="Utah Jazz", away_display_name="Denver Nuggets", home_id="26",
               away_id="7", home_score="124", away_score="132", season="2020", season_type="2")
    body = (",".join(cols) + "\n" + ",".join(row[c] for c in cols) + "\n").encode()
    [ev] = parse_schedule(body, sport="nba", fetched_at=datetime(2026, 1, 1, tzinfo=UTC)).events
    assert ev.season == 2019  # 2019-20, not 2020-21 as the calendar would say
