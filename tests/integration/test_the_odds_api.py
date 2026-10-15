"""The Odds API -> NFL, end to end against recorded-format responses."""

from urllib.parse import parse_qs

import pytest
from sqlalchemy import func, select

from betdata.db.schema import (
    api_quota,
    api_usage,
    books,
    game_source_ids,
    games,
    ingest_runs,
    odds_snapshots,
    raw_responses,
    teams,
)
from betdata.sources.the_odds_api import TheOddsApiSource, estimate_cost
from tests.conftest import fixture_bytes

HEADERS_1 = {"x-requests-last": "3", "x-requests-remaining": "497", "x-requests-used": "3"}
HEADERS_2 = {"x-requests-last": "3", "x-requests-remaining": "494", "x-requests-used": "6"}


@pytest.fixture
def source(engine, settings, fake_api, clock):
    return TheOddsApiSource(
        engine,
        settings.source("the_odds_api"),
        transport=fake_api.transport,
        sleep=lambda s: None,
        clock=clock,
    )


def count(engine, table, *where):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table).where(*where)).scalar()


def snapshots(engine):
    with engine.connect() as conn:
        return (
            conn.execute(
                select(odds_snapshots, books.c.key.label("book"))
                .join(books)
                .order_by(odds_snapshots.c.snapshot_id)
            )
            .mappings()
            .all()
        )


def test_cost_estimate():
    assert estimate_cost(["h2h", "spreads", "totals"], ["us"], None) == 3
    assert estimate_cost(["h2h"], ["us", "eu"], None) == 2
    assert estimate_cost(["h2h", "spreads"], None, [f"b{i}" for i in range(11)]) == 4


def test_request_shape(source, fake_api):
    fake_api.add(body=fixture_bytes("the_odds_api", "nfl_odds_snapshot1.json"), headers=HEADERS_1)
    source.run("odds", sport="nfl")
    req = fake_api.requests[0]
    q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
    assert req.url.path == "/v4/sports/americanfootball_nfl/odds"
    assert q == {
        "markets": "h2h,spreads,totals",
        "oddsFormat": "american",
        "dateFormat": "iso",
        "regions": "us",
        "apiKey": "test-key-123",
    }


def test_first_snapshot_creates_games_and_all_prices(source, engine, fake_api):
    fake_api.add(body=fixture_bytes("the_odds_api", "nfl_odds_snapshot1.json"), headers=HEADERS_1)
    result = source.run("odds", sport="nfl")

    assert result.status == "ok" and result.rows_inserted == 18 and result.rows_seen == 18
    assert count(engine, games) == 2 and count(engine, game_source_ids) == 2
    rows = snapshots(engine)
    assert all(r["game_id"] is not None and r["price_status"] == "active" for r in rows)

    with engine.connect() as conn:
        g = conn.execute(
            select(games.c.season, teams.c.canonical_name)
            .join(teams, teams.c.team_id == games.c.home_team_id)
            .order_by(games.c.start_time_utc.desc())
        ).first()
    assert g.season == 2026 and g.canonical_name == "Las Vegas Raiders"

    dk_spread_home = next(
        r for r in rows if r["book"] == "draftkings" and r["outcome"] == "home" and r["line"] == 9.5
    )
    assert dk_spread_home["price_american"] == -110
    assert dk_spread_home["price_decimal"] == pytest.approx(1.909091)
    ml = [r for r in rows if r["line"] is None]
    assert len(ml) == 6  # h2h has no line


def test_quota_logged_and_reconciled(source, engine, fake_api):
    fake_api.add(body=fixture_bytes("the_odds_api", "nfl_odds_snapshot1.json"), headers=HEADERS_1)
    source.run("odds", sport="nfl")
    with engine.connect() as conn:
        u = conn.execute(select(api_usage)).mappings().one()
        q = conn.execute(select(api_quota)).mappings().one()
    assert u["cost_units"] == 3 and u["cost_reported"] and u["remaining_reported"] == 497
    assert q["remaining_reported"] == 497
    assert source.quota.status().used_effective == 3


def test_second_snapshot_stores_only_changes_and_flags_removed(source, engine, fake_api, clock):
    fake_api.add(body=fixture_bytes("the_odds_api", "nfl_odds_snapshot1.json"), headers=HEADERS_1)
    source.run("odds", sport="nfl")
    clock.advance(days=1)
    fake_api.add(body=fixture_bytes("the_odds_api", "nfl_odds_snapshot2.json"), headers=HEADERS_2)
    result = source.run("odds", sport="nfl")

    new = [r for r in snapshots(engine) if r["run_id"] == result.run_id]
    assert result.rows_inserted == 4 and len(new) == 4
    moved = sorted((r["outcome"], r["line"]) for r in new if r["price_status"] == "active")
    assert moved == [("away", -10.0), ("home", 10.0)]
    removed = [r for r in new if r["price_status"] == "removed"]
    assert {(r["book"], r["outcome"]) for r in removed} == {
        ("fanduel", "home"),
        ("fanduel", "away"),
    }
    assert {r["price_american"] for r in removed} == {-440, 350}  # last known prices kept
    # history intact: the original 18 rows are untouched
    assert count(engine, odds_snapshots) == 22


def test_unchanged_poll_inserts_nothing_but_is_logged(source, engine, fake_api, clock):
    body = fixture_bytes("the_odds_api", "nfl_odds_snapshot1.json")
    fake_api.add(body=body, headers=HEADERS_1).add(body=body, headers=HEADERS_2)
    source.run("odds", sport="nfl")
    clock.advance(hours=1)
    r2 = source.run("odds", sport="nfl", priority="high")
    assert r2.status == "ok" and r2.rows_inserted == 0 and r2.rows_seen == 18
    assert count(engine, ingest_runs, ingest_runs.c.status == "ok") == 2


def test_reparse_is_idempotent(source, engine, fake_api):
    fake_api.add(body=fixture_bytes("the_odds_api", "nfl_odds_snapshot1.json"), headers=HEADERS_1)
    source.run("odds", sport="nfl")
    raw_id = snapshots(engine)[0]["raw_id"]
    assert source.reparse(raw_id) == (0, 18)
    assert count(engine, odds_snapshots) == 18
    assert count(engine, api_usage) == 1  # no API call made


def test_unknown_team_keeps_odds_with_null_game(source, engine, fake_api):
    body = fixture_bytes("the_odds_api", "nfl_odds_snapshot1.json").replace(
        b"New York Jets", b"Gotham Rogues"
    )
    fake_api.add(body=body, headers=HEADERS_1)
    result = source.run("odds", sport="nfl")
    assert result.status == "ok" and result.rows_inserted == 18
    assert count(engine, games) == 1
    assert count(engine, odds_snapshots, odds_snapshots.c.game_id.is_(None)) == 6


def test_format_change_fails_job_but_keeps_raw(source, engine, fake_api):
    fake_api.add(body=b'{"message": "new format"}', headers=HEADERS_1)
    result = source.run("odds", sport="nfl")
    assert result.status == "failed" and "ParseError" in result.message
    assert count(engine, raw_responses) == 1 and count(engine, odds_snapshots) == 0


def test_quota_exhaustion_skips_without_calling(source, engine, fake_api, clock):
    source.quota.reconcile(remaining=52, used=448)  # 450 usable -> 2 left
    result = source.run("odds", sport="nfl", priority="high")
    assert result.status == "skipped" and "QuotaExceeded" in result.message
    assert fake_api.requests == []


def test_events_job_is_free_and_creates_games(source, engine, fake_api):
    fake_api.add(
        body=fixture_bytes("the_odds_api", "nfl_events.json"),
        headers={"x-requests-last": "0", "x-requests-remaining": "500"},
    )
    result = source.run("events", sport="nfl")
    assert result.status == "ok" and count(engine, games) == 2
    assert source.quota.status().used_effective == 0
    # odds for the same events attach to those games instead of creating new ones
    fake_api.add(body=fixture_bytes("the_odds_api", "nfl_odds_snapshot1.json"), headers=HEADERS_1)
    source.run("odds", sport="nfl")
    assert count(engine, games) == 2
