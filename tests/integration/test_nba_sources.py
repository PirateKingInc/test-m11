"""M7: NBA schedule/results from nba_cdn (authority) and sportsdataverse (fallback), and
college basketball results from sportsdataverse attaching to CFBD games by ESPN id."""

import pytest
from sqlalchemy import func, select

from betdata.db.schema import (
    game_source_ids,
    games,
    raw_responses,
    results,
    teams,
    unmatched_entities,
)
from betdata.sources.cfbd import CfbdSource
from betdata.sources.nba_cdn import NbaCdnSource
from betdata.sources.sportsdataverse import SportsDataverseSource
from tests.conftest import fixture_bytes


@pytest.fixture
def cdn(engine, settings, fake_api, clock):
    return NbaCdnSource(engine, settings.source("nba_cdn"), transport=fake_api.transport,
                        sleep=lambda s: None, clock=clock)  # fmt: skip


@pytest.fixture
def sdv(engine, settings, fake_api, clock):
    return SportsDataverseSource(
        engine,
        settings.source("sportsdataverse"),
        transport=fake_api.transport,
        sleep=lambda s: None,
        clock=clock,
    )


def count(engine, table, *where):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table).where(*where)).scalar()


def game_by(engine, source, gid):
    with engine.connect() as conn:
        return conn.execute(
            select(games).join(game_source_ids).where(
                game_source_ids.c.source == source, game_source_ids.c.source_game_id == gid
            )
        ).mappings().one()  # fmt: skip


def abbrev(engine, team_id):
    with engine.connect() as conn:
        return conn.execute(select(teams.c.abbrev).where(teams.c.team_id == team_id)).scalar()


# --- nba_cdn -------------------------------------------------------------------------


def test_cdn_schedule(cdn, engine, fake_api):
    fake_api.add(body=fixture_bytes("nba_cdn", "scheduleLeagueV2.json"))
    result = cdn.run("schedule")
    assert result.status == "ok", result.message
    req = fake_api.requests[0]
    assert str(req.url) == "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
    assert req.headers["referer"] == "https://www.nba.com/"
    assert result.rows_seen == 4  # All-Star skipped
    pre = game_by(engine, "nba_cdn", "0012600005")
    assert (pre["season_type"], pre["status"], pre["season"]) == ("PRE", "final", 2026)
    assert abbrev(engine, pre["home_team_id"]) == "BOS"
    assert game_by(engine, "nba_cdn", "0022600640")["status"] == "postponed"
    assert game_by(engine, "nba_cdn", "0022600641")["neutral_site"] is True
    assert count(engine, results) == 1
    assert count(engine, unmatched_entities) == 0


def test_cdn_scoreboard_same_night_final(cdn, engine, fake_api):
    fake_api.add(body=fixture_bytes("nba_cdn", "scheduleLeagueV2.json"))
    cdn.run("schedule")
    fake_api.add(body=fixture_bytes("nba_cdn", "todaysScoreboard_00.json"))
    assert cdn.run("scoreboard").status == "ok"
    opener = game_by(engine, "nba_cdn", "0022600001")
    assert opener["status"] == "final"
    with engine.connect() as conn:
        res = (
            conn.execute(select(results).where(results.c.game_id == opener["game_id"]))
            .mappings()
            .one()
        )
    assert (res["home_score"], res["away_score"]) == (108, 112)


def test_cdn_takes_over_games_the_fallback_created(cdn, sdv, engine, fake_api):
    fake_api.add(body=fixture_bytes("sportsdataverse", "nba_schedule_2027.csv"))
    assert sdv.run("schedule", sport="nba").status == "ok"
    opener = game_by(engine, "sportsdataverse", "401909088")
    assert opener["schedule_source"] == "sportsdataverse"

    fake_api.add(body=fixture_bytes("nba_cdn", "scheduleLeagueV2.json"))
    cdn.run("schedule")
    same = game_by(engine, "nba_cdn", "0022600001")
    assert same["game_id"] == opener["game_id"]
    assert same["schedule_source"] == "nba_cdn"


def test_cdn_shape_change_fails_cleanly(cdn, fake_api, engine):
    fake_api.add(body=b'{"meta": {}, "somethingElse": {}}')
    result = cdn.run("schedule")
    assert result.status == "failed" and "ParseError" in result.message
    assert count(engine, raw_responses) == 1


# --- sportsdataverse: NBA -------------------------------------------------------------


def test_sdv_nba_past_season(sdv, engine, fake_api):
    fake_api.add(body=fixture_bytes("sportsdataverse", "nba_schedule_2026.csv"))
    result = sdv.run("schedule", sport="nba", seasons="2025")
    assert result.status == "ok", result.message
    path = fake_api.requests[0].url.path
    assert path.endswith("/espn_nba_schedules/nba_schedule_2026.csv")  # 2025-26 season
    assert result.rows_seen == 5  # All-Star exhibition dropped, never queued for review
    assert count(engine, unmatched_entities) == 0
    assert game_by(engine, "sportsdataverse", "401810507")["status"] == "postponed"
    assert game_by(engine, "sportsdataverse", "401810457")["neutral_site"] is True
    finals = game_by(engine, "sportsdataverse", "401859967")
    assert (finals["season"], finals["season_type"]) == (2025, "POST")
    assert game_by(engine, "sportsdataverse", "401866759")["season_type"] == "POST"  # play-in
    assert count(engine, results) == 4
    with engine.connect() as conn:
        res = (
            conn.execute(select(results).where(results.c.game_id == finals["game_id"]))
            .mappings()
            .one()
        )
    assert (res["home_score"], res["away_score"]) == (90, 94)  # Spurs 90, Knicks 94


def test_sdv_default_is_current_season_and_skips_tbd(sdv, engine, fake_api):
    fake_api.add(body=fixture_bytes("sportsdataverse", "nba_schedule_2027.csv"))
    result = sdv.run("schedule", sport="nba")
    assert fake_api.requests[0].url.path.endswith("nba_schedule_2027.csv")  # clock: Oct 2026
    assert result.rows_seen == 1 and count(engine, unmatched_entities) == 0


def test_sdv_unchanged_file_not_rearchived(sdv, engine, fake_api, clock):
    body = fixture_bytes("sportsdataverse", "nba_schedule_2027.csv")
    fake_api.add(body=body).add(body=body)
    sdv.run("schedule", sport="nba")
    clock.advance(days=1)
    sdv.run("schedule", sport="nba")
    assert count(engine, raw_responses) == 1


# --- sportsdataverse: college basketball ----------------------------------------------


@pytest.fixture
def cbbd(engine, settings, fake_api, clock):
    src = CfbdSource(engine, settings.source("cfbd"), transport=fake_api.transport,
                     sleep=lambda s: None, clock=clock)  # fmt: skip
    for job in ("teams_basketball", "games_basketball"):
        fake_api.add(body=fixture_bytes("cfbd", f"{job}.json"))
        assert src.run(job).status == "ok"
    return src


def test_sdv_ncaab_joins_cfbd_games_by_espn_id(cbbd, sdv, engine, fake_api):
    before = count(engine, games)
    fake_api.add(body=fixture_bytes("sportsdataverse", "mbb_schedule_2027.csv"))
    result = sdv.run("schedule", sport="ncaab")
    assert result.status == "ok", result.message
    assert result.rows_seen == 2  # non-D-I opponent skipped
    uconn = game_by(engine, "cfbd", "ncaab:9001")
    assert game_by(engine, "sportsdataverse", "401800001")["game_id"] == uconn["game_id"]
    assert count(engine, games) == before + 1  # Saint Mary's - St. John's is new
    with engine.connect() as conn:
        sources = (
            conn.execute(select(results.c.source).where(results.c.game_id == uconn["game_id"]))
            .scalars()
            .all()
        )
    assert sorted(sources) == ["cfbd", "sportsdataverse"]
    assert count(engine, unmatched_entities) == 0


def test_sdv_ncaab_needs_team_import(sdv, fake_api):
    fake_api.add(body=fixture_bytes("sportsdataverse", "mbb_schedule_2027.csv"))
    result = sdv.run("schedule", sport="ncaab")
    assert result.status == "failed" and "teams import" in result.message
