"""nflverse schedules: parsing, loading, closing lines, results, and cross-source matching."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text

from betdata.db.schema import (
    books,
    game_source_ids,
    games,
    odds_snapshots,
    raw_responses,
    results,
    teams,
)
from betdata.sources.nflverse import NflverseSource, parse_schedules, parse_seasons
from betdata.sources.the_odds_api import TheOddsApiSource
from tests.conftest import fixture_bytes

SAMPLE = fixture_bytes("nflverse", "games_sample.csv")
ODDS_HEADERS = {"x-requests-last": "3", "x-requests-remaining": "497", "x-requests-used": "3"}


@pytest.fixture
def nflverse(engine, settings, fake_api, clock):
    return NflverseSource(
        engine, settings.source("nflverse"), transport=fake_api.transport,
        sleep=lambda s: None, clock=clock,
    )  # fmt: skip


@pytest.fixture
def odds_api(engine, settings, fake_api, clock):
    return TheOddsApiSource(
        engine, settings.source("the_odds_api"), transport=fake_api.transport,
        sleep=lambda s: None, clock=clock,
    )  # fmt: skip


def q(engine, stmt):
    with engine.connect() as conn:
        return conn.execute(stmt).mappings().all()


def count(engine, table, *where):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table).where(*where)).scalar()


def edit_row(body: bytes, game_id: str, **changes) -> bytes:
    lines = body.decode().splitlines()
    header = lines[0].split(",")
    for i, line in enumerate(lines):
        if line.startswith(game_id + ","):
            cells = line.split(",")
            for k, v in changes.items():
                cells[header.index(k)] = str(v)
            lines[i] = ",".join(cells)
    return ("\n".join(lines) + "\n").encode()


def odds_event(event_id, home, away, commence, spread_home=-2.5):
    outcomes = [{"name": away, "price": -110, "point": -spread_home},
                {"name": home, "price": -110, "point": spread_home}]  # fmt: skip
    return {
        "id": event_id, "sport_key": "americanfootball_nfl", "commence_time": commence,
        "home_team": home, "away_team": away,
        "bookmakers": [{"key": "draftkings", "title": "DraftKings", "last_update": commence,
                        "markets": [{"key": "spreads", "outcomes": outcomes}]}],
    }  # fmt: skip


def game_for(engine, source, source_game_id):
    return q(
        engine,
        select(games, game_source_ids.c.home_away_swapped)
        .join(game_source_ids)
        .where(
            game_source_ids.c.source == source, game_source_ids.c.source_game_id == source_game_id
        ),
    )[0]


# --- parsing ----------------------------------------------------------------


def test_parse_seasons():
    now = datetime(2026, 10, 15, tzinfo=UTC)
    assert parse_seasons(None, now=now) == {2026}
    assert parse_seasons(None, now=datetime(2027, 1, 20, tzinfo=UTC)) == {2026}
    assert parse_seasons("2020-2022,2025") == {2020, 2021, 2022, 2025}
    assert parse_seasons("all") is None


def test_parse_kickoffs_spreads_and_kinds():
    batch = parse_schedules(SAMPLE, fetched_at=datetime(2026, 10, 15, tzinfo=UTC), seasons=None)
    ev = {e.source_game_id: e for e in batch.events}
    assert len(ev) == 8 and batch.schedule_authority
    # Eastern -> UTC across DST, and the default kickoff for old games without a time
    assert ev["2026_06_HOU_JAX"].start_time_utc == datetime(2026, 10, 18, 13, 30, tzinfo=UTC)
    assert ev["2024_22_KC_PHI"].start_time_utc == datetime(2025, 2, 9, 23, 30, tzinfo=UTC)
    assert ev["1999_01_MIN_ATL"].start_time_utc == datetime(1999, 9, 12, 17, 0, tzinfo=UTC)
    sb = ev["2024_22_KC_PHI"]
    assert (sb.season_type, sb.week, sb.neutral_site, sb.final) == ("POST", 22, True, True)
    assert (sb.home_score, sb.away_score) == (40, 22)

    prices = {(p.source_game_id, p.market_key, p.outcome): p for p in batch.prices}
    # spread_line 8.5 = PHI (home) favored by 8.5
    assert prices[("2025_01_DAL_PHI", "spreads", "home")].line == -8.5
    assert prices[("2025_01_DAL_PHI", "spreads", "away")].line == 8.5
    assert prices[("2025_01_DAL_PHI", "h2h", "home")].price_american == -425
    assert prices[("2025_01_DAL_PHI", "h2h", "home")].snapshot_kind == "source_closing"
    assert prices[("2026_06_SEA_DEN", "totals", "over")].snapshot_kind == "poll"
    # 1999: spread and total but no prices, no moneyline
    old = [p for p in batch.prices if p.source_game_id == "1999_01_MIN_ATL"]
    assert {(p.market_key, p.outcome, p.line) for p in old} == {
        ("spreads", "home", 4.0), ("spreads", "away", -4.0),
        ("totals", "over", 49.0), ("totals", "under", 49.0),
    }  # fmt: skip
    assert all(p.price_american is None for p in old)
    assert not [p for p in batch.prices if p.source_game_id == "2026_18_DAL_WAS"]


def test_missing_column_is_parse_error(nflverse, fake_api):
    fake_api.add(body=SAMPLE.replace(b"spread_line", b"spread"))
    result = nflverse.run("schedules", seasons="all")
    assert result.status == "failed" and "spread_line" in result.message


# --- loading ----------------------------------------------------------------


def test_backfill_all_seasons(nflverse, engine, fake_api):
    fake_api.add(body=SAMPLE)
    result = nflverse.run("schedules", seasons="all")
    assert result.status == "ok"
    assert count(engine, games) == 8
    assert count(engine, games, games.c.schedule_source == "nflverse") == 8
    assert count(engine, results) == 4 and count(engine, games, games.c.status == "final") == 4
    sb = game_for(engine, "nflverse", "2024_22_KC_PHI")
    assert (sb["season"], sb["season_type"], sb["week"], sb["neutral_site"]) == (
        2024,
        "POST",
        22,
        True,
    )
    # old team code resolves to the current franchise
    old = game_for(engine, "nflverse", "1999_01_MIN_ATL")
    assert old["season"] == 1999
    closing = q(
        engine,
        select(odds_snapshots, books.c.key.label("book"))
        .join(books)
        .where(odds_snapshots.c.source_game_id == "2025_01_DAL_PHI"),
    )
    assert len(closing) == 6
    assert {r["snapshot_kind"] for r in closing} == {"source_closing"}
    assert {r["book"] for r in closing} == {"nflverse"}


def test_default_loads_current_season_only(nflverse, engine, fake_api):
    fake_api.add(body=SAMPLE)
    nflverse.run("schedules")
    assert count(engine, games) == 4  # the 2026 rows


def test_unchanged_file_is_not_archived_twice(nflverse, engine, fake_api, clock):
    fake_api.add(body=SAMPLE).add(body=SAMPLE)
    nflverse.run("schedules", seasons="all")
    clock.advance(days=1)
    r2 = nflverse.run("schedules", seasons="all")
    assert r2.status == "ok" and r2.rows_inserted == 0
    assert count(engine, raw_responses) == 1


def test_line_moves_then_game_goes_final(nflverse, engine, fake_api, clock):
    gid = "2026_06_SEA_DEN"
    fake_api.add(body=SAMPLE)
    nflverse.run("schedules")
    clock.advance(hours=12)
    fake_api.add(body=edit_row(SAMPLE, gid, spread_line="-3", home_spread_odds="-110"))
    r2 = nflverse.run("schedules")
    assert r2.rows_inserted == 2  # only the two spread selections changed

    clock.advance(days=1)
    final = edit_row(SAMPLE, gid, spread_line="-3", home_score="27", away_score="20")
    fake_api.add(body=final)
    nflverse.run("schedules")
    rows = q(engine, select(odds_snapshots).where(odds_snapshots.c.source_game_id == gid))
    kinds = [r["snapshot_kind"] for r in rows]
    assert kinds.count("poll") == 8 and kinds.count("source_closing") == 6
    g = game_for(engine, "nflverse", gid)
    res = q(engine, select(results).where(results.c.game_id == g["game_id"]))[0]
    assert (res["home_score"], res["away_score"], res["final"], g["status"]) == (
        27,
        20,
        True,
        "final",
    )


# --- cross-source matching ---------------------------------------------------


def run_odds(odds_api, fake_api, events):
    fake_api.add(body=json.dumps(events), headers=ODDS_HEADERS)
    assert odds_api.run("odds", sport="nfl", priority="high").status == "ok"


LONDON = odds_event("oa_houjax", "Houston Texans", "Jacksonville Jaguars", "2026-10-18T13:30:00Z")
ATLANTA = odds_event("oa_chiatl", "Atlanta Falcons", "Chicago Bears", "2026-10-18T17:00:00Z")


def test_odds_first_then_nflverse_merges_and_takes_orientation(
    odds_api, nflverse, engine, fake_api
):
    # The Odds API lists the London game with HOU as home; nflverse says JAX is home.
    run_odds(odds_api, fake_api, [LONDON, ATLANTA])
    assert count(engine, games) == 2
    fake_api.add(body=SAMPLE)
    nflverse.run("schedules")

    assert count(engine, games) == 4  # 2 matched, 2 new (SEA_DEN, DAL_WAS)
    london = game_for(engine, "the_odds_api", "oa_houjax")
    assert london["home_away_swapped"] is True
    assert london["schedule_source"] == "nflverse" and london["week"] == 6
    with engine.connect() as conn:
        home = conn.execute(select(teams.c.abbrev).where(teams.c.team_id == london["home_team_id"]))
        assert home.scalar() == "JAX"
        assert game_for(engine, "nflverse", "2026_06_HOU_JAX")["game_id"] == london["game_id"]
        # Stored odds keep the source's orientation; the view presents the game's.
        rows = conn.execute(
            text(
                "SELECT outcome, game_outcome, line FROM v_odds_snapshots "
                "WHERE source='the_odds_api' AND source_game_id='oa_houjax' ORDER BY outcome"
            )
        ).all()
    # Odds API "home" = HOU at -2.5 -> the game's away team (HOU) at -2.5
    assert rows == [("away", "home", 2.5), ("home", "away", -2.5)]
    atl = game_for(engine, "the_odds_api", "oa_chiatl")
    assert atl["home_away_swapped"] is False


def test_nflverse_first_then_odds_attaches_without_new_games(odds_api, nflverse, engine, fake_api):
    fake_api.add(body=SAMPLE)
    nflverse.run("schedules")
    run_odds(odds_api, fake_api, [LONDON, ATLANTA])
    assert count(engine, games) == 4
    london = game_for(engine, "the_odds_api", "oa_houjax")
    assert london["home_away_swapped"] is True  # non-authoritative: game keeps JAX home
    assert london["start_time_utc"] == datetime(2026, 10, 18, 13, 30, tzinfo=UTC)


def test_odds_api_cannot_move_a_game_nflverse_owns(odds_api, nflverse, engine, fake_api):
    fake_api.add(body=SAMPLE)
    nflverse.run("schedules")
    moved = odds_event("oa_chiatl", "Atlanta Falcons", "Chicago Bears", "2026-10-18T20:25:00Z")
    run_odds(odds_api, fake_api, [moved])
    g = game_for(engine, "nflverse", "2026_06_CHI_ATL")
    assert g["start_time_utc"] == datetime(2026, 10, 18, 17, 0, tzinfo=UTC)
