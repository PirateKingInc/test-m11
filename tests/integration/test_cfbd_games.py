"""M5: CFBD/CBBD games, lines and results."""

import json
import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from betdata.core.matching import sport_id
from betdata.db.schema import (
    books,
    game_source_ids,
    games,
    markets,
    odds_snapshots,
    raw_responses,
    results,
    teams,
)
from betdata.scheduler.jobs import Gate, gate_open
from betdata.sources.cfbd import CfbdSource, backfill_plan
from betdata.sources.cfbd_games import _formatted_home_spread
from betdata.sources.the_odds_api import TheOddsApiSource
from tests.conftest import fixture_bytes


@pytest.fixture
def cfbd(engine, settings, fake_api, clock):
    return CfbdSource(
        engine, settings.source("cfbd"), transport=fake_api.transport,
        sleep=lambda s: None, clock=clock,
    )  # fmt: skip


def run(cfbd, fake_api, job, fixture=None, **params):
    fake_api.add(body=fixture_bytes("cfbd", fixture or f"{job}.json"))
    result = cfbd.run(job, **params)
    assert result.status == "ok", result.message
    return result


def count(engine, table, *where):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table).where(*where)).scalar()


def game(engine, cfbd_id, sport="ncaaf"):
    with engine.connect() as conn:
        return (
            conn.execute(
                select(games, game_source_ids.c.source_game_id)
                .join(game_source_ids)
                .where(
                    game_source_ids.c.source == "cfbd",
                    game_source_ids.c.source_game_id == f"{sport}:{cfbd_id}",
                )  # fmt: skip
            )
            .mappings()
            .one()
        )


def lines_for(engine, cfbd_id, sport="ncaaf", **where):
    with engine.connect() as conn:
        q = (
            select(odds_snapshots, books.c.key.label("book"), markets.c.key.label("market"))
            .join(books)
            .join(markets)
            .where(odds_snapshots.c.source_game_id == f"{sport}:{cfbd_id}")
        )
        for k, v in where.items():
            q = q.where(odds_snapshots.c[k] == v)
        return conn.execute(q).mappings().all()


def by(rows, book, market, outcome, kind):
    want = (book, market, outcome, kind)
    hits = [r for r in rows if (r["book"], r["market"], r["outcome"], r["snapshot_kind"]) == want]
    assert len(hits) == 1, (want, hits)
    return hits[0]


@pytest.fixture
def football(cfbd, fake_api):
    run(cfbd, fake_api, "teams_football")
    return cfbd


@pytest.fixture
def basketball(cfbd, fake_api):
    run(cfbd, fake_api, "teams_basketball")
    return cfbd


# --- football --------------------------------------------------------------------


def test_football_games_schedule_and_results(football, engine, fake_api):
    result = run(football, fake_api, "games_football")
    q = fake_api.requests[-1].url.params
    assert (q["year"], q["seasonType"], q["classification"]) == ("2026", "both", "fbs")
    assert result.rows_seen == 5  # D-II opponent skipped
    assert count(engine, games) == 5 and count(engine, results) == 3
    usc = game(engine, 401700003)
    assert (usc["neutral_site"], usc["week"], usc["season_type"], usc["status"]) == (
        True,
        3,
        "REG",
        "final",
    )
    assert usc["schedule_source"] == "cfbd"
    with engine.connect() as conn:
        res = (
            conn.execute(select(results).where(results.c.game_id == usc["game_id"]))
            .mappings()
            .one()
        )
    assert (res["home_score"], res["away_score"]) == (24, 27)
    assert game(engine, 401700006)["status"] == "scheduled"


def test_football_lines_kinds_and_orientation(football, engine, fake_api):
    run(football, fake_api, "games_football")
    run(football, fake_api, "lines_football")
    assert count(engine, games) == 5  # lines attach to existing games

    bama = lines_for(engine, 401700002)  # final: closing + opening, no moneyline
    assert by(bama, "consensus", "spreads", "home", "source_closing")["line"] == -45.5
    assert by(bama, "consensus", "spreads", "away", "source_closing")["line"] == 45.5
    assert by(bama, "consensus", "spreads", "home", "source_opening")["line"] == -44
    assert by(bama, "consensus", "totals", "over", "source_closing")["line"] == 55.5
    assert by(bama, "consensus", "totals", "over", "source_closing")["price_american"] is None
    assert not [r for r in bama if r["market"] == "h2h"]

    # USC home, South Carolina favored by 3.5 -> USC +3.5, for both providers, even though
    # Bovada's raw `spread` is inverted
    usc = lines_for(engine, 401700003)
    assert by(usc, "consensus", "spreads", "home", "source_closing")["line"] == 3.5
    assert by(usc, "bovada", "spreads", "home", "source_closing")["line"] == 3.5
    assert by(usc, "bovada", "h2h", "away", "source_closing")["price_american"] == -170

    # upcoming game: current numbers are polls; DraftKings maps to the seeded book
    upcoming = lines_for(engine, 401700006)
    dk = by(upcoming, "draftkings", "spreads", "home", "poll")
    assert dk["line"] == -24.5
    assert by(upcoming, "draftkings", "h2h", "home", "poll")["price_american"] == -2500


def test_line_movement_is_appended(football, engine, fake_api, clock):
    run(football, fake_api, "games_football")
    run(football, fake_api, "lines_football")
    before = count(engine, odds_snapshots)
    clock.advance(days=1)
    moved = (
        fixture_bytes("cfbd", "lines_football.json")
        .replace(b"-24.5", b"-26.0")
        .replace(b'"Alabama -24.5"', b'"Alabama -26"')
    )
    fake_api.add(body=moved)
    result = football.run("lines_football")
    assert result.rows_inserted == 2  # home and away spread polls
    assert count(engine, odds_snapshots) == before + 2


def test_odds_api_event_matches_cfbd_game(football, engine, fake_api, settings, clock):
    run(football, fake_api, "games_football")
    odds = TheOddsApiSource(engine, settings.source("the_odds_api"),
                            transport=fake_api.transport, clock=clock)  # fmt: skip
    board = [{
        "id": "oa1", "sport_key": "americanfootball_ncaaf",
        "commence_time": "2026-10-17T23:30:00Z",
        "home_team": "Alabama Crimson Tide", "away_team": "San Jose State Spartans",
        "bookmakers": [{"key": "fanduel", "title": "FanDuel", "markets": [{"key": "h2h",
            "outcomes": [{"name": "Alabama Crimson Tide", "price": -3000},
                         {"name": "San Jose State Spartans", "price": 1300}]}]}],
    }]  # fmt: skip
    fake_api.add(body=json.dumps(board), headers={"x-requests-last": "1"})
    assert odds.run("odds", sport="ncaaf", markets=["h2h"]).status == "ok"
    assert count(engine, games) == 5
    with engine.connect() as conn:
        ids = (
            conn.execute(
                select(game_source_ids.c.source).where(
                    game_source_ids.c.game_id == game(engine, 401700006)["game_id"]
                )
            )
            .scalars()
            .all()
        )
    assert sorted(ids) == ["cfbd", "the_odds_api"]


def test_requires_team_import(cfbd, fake_api, engine):
    fake_api.add(body=fixture_bytes("cfbd", "games_football.json"))
    result = cfbd.run("games_football")
    assert result.status == "failed" and "teams import" in result.message
    assert count(engine, raw_responses) == 1  # the response is kept for a later reparse


# --- basketball ------------------------------------------------------------------


def test_basketball_window_status_and_espn_ids(basketball, engine, fake_api):
    result = run(basketball, fake_api, "games_basketball")
    q = fake_api.requests[-1].url.params
    assert q["startDateRange"] == "2026-10-12T00:00:00Z"  # clock is 2026-10-15
    assert q["endDateRange"] == "2026-10-23T23:59:59Z"
    assert result.rows_seen == 3  # non-D-I opponent skipped, not queued for review
    assert game(engine, 9002, "ncaab")["status"] == "postponed"
    final = game(engine, 9001, "ncaab")
    assert final["status"] == "final" and final["season"] == 2026
    with engine.connect() as conn:
        espn = conn.execute(
            select(game_source_ids.c.game_id).where(
                game_source_ids.c.source == "espn", game_source_ids.c.source_game_id == "401800001"
            )
        ).scalar()
        n_teams = conn.execute(select(func.count()).select_from(teams)).scalar()
    assert espn == final["game_id"]
    assert n_teams == 32 + 30 + 6  # no stray team rows created for the non-D-I opponent


def test_basketball_lines_and_sanity_check(basketball, engine, fake_api, caplog):
    run(basketball, fake_api, "games_basketball")
    with caplog.at_level(logging.WARNING):
        run(basketball, fake_api, "lines_basketball", start="2026-11-01", end="2026-11-30")
    q = fake_api.requests[-1].url.params
    assert (q["startDateRange"], q["endDateRange"]) == (
        "2026-11-01T00:00:00Z",
        "2026-11-30T23:59:59Z",
    )
    uconn = lines_for(engine, 9001, "ncaab")
    assert by(uconn, "espnbet", "spreads", "home", "source_closing")["line"] == -2.5
    assert by(uconn, "espnbet", "spreads", "home", "source_opening")["line"] == -1.5
    miami = lines_for(engine, 9003, "ncaab")
    assert by(miami, "draftkings", "spreads", "home", "poll")["line"] == 6.5
    # Bovada's spread says Miami is favored but its moneylines say Miami (OH): flagged
    assert "disagree on the favorite" in caplog.text and "bovada" in caplog.text.lower()


def test_postponed_games_do_not_open_odds_gate(basketball, engine, fake_api):
    run(basketball, fake_api, "games_basketball")
    before_postponed = datetime(2026, 11, 12, 2, 0, tzinfo=UTC)
    assert not gate_open(engine, Gate("ncaab", 0, 120), before_postponed)
    before_scheduled = datetime(2026, 11, 19, 23, 0, tzinfo=UTC)
    assert gate_open(engine, Gate("ncaab", 0, 120), before_scheduled)


def test_unchanged_response_not_archived_twice(basketball, engine, fake_api, clock):
    run(basketball, fake_api, "games_basketball")
    clock.advance(hours=1)
    run(basketball, fake_api, "games_basketball")
    with engine.connect() as conn:
        n = conn.execute(
            select(func.count())
            .select_from(raw_responses)
            .where(raw_responses.c.endpoint == "basketball:/games")
        ).scalar()
    assert n == 1


# --- backfill ----------------------------------------------------------------------


def test_backfill_plan_sizes():
    assert len(backfill_plan("ncaaf", [2023, 2024])) == 4
    plan = backfill_plan("ncaab", [2025])
    assert len(plan) == 12
    assert plan[0] == ("games_basketball", {"start": "2025-10-25", "end": "2025-11-30"})
    assert plan[-1] == ("lines_basketball", {"start": "2026-04-01", "end": "2026-04-15"})


def test_backfill_skips_windows_already_fetched(football, fake_api):
    plan = backfill_plan("ncaaf", [2026])
    assert not any(football.already_fetched(j, p) for j, p in plan)
    run(football, fake_api, "games_football", season=2026)
    assert football.already_fetched(*plan[0]) and not football.already_fetched(*plan[1])


def test_formatted_spread_parsing():
    assert _formatted_home_spread("Georgia -21.5", "Georgia", "Clemson") == -21.5
    assert _formatted_home_spread("Georgia -21.5", "Clemson", "Georgia") == 21.5
    assert _formatted_home_spread("Texas A&M -3", "Texas A&M", "LSU") == -3
    assert _formatted_home_spread("PK", "A", "B") == 0.0
    assert _formatted_home_spread("Somebody Else -3", "A", "B") is None
    assert _formatted_home_spread(None, "A", "B") is None


def test_quota_shared_between_football_and_basketball(cfbd, fake_api):
    run(cfbd, fake_api, "teams_football")
    run(cfbd, fake_api, "teams_basketball")
    assert cfbd.quota.status().used_effective == 2


def test_sport_ids(engine):
    with engine.connect() as conn:
        assert sport_id(conn, "ncaab") != sport_id(conn, "ncaaf")
