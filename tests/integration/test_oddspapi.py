"""M6: OddsPapi archive-first board parsing, mapping, reparse, not-configured skips."""

import dataclasses
import logging

import pytest
from sqlalchemy import func, select

from betdata.db.schema import books, odds_snapshots, raw_responses
from betdata.sources.oddspapi import OddsPapiSource
from tests.conftest import fixture_bytes

BOARD = fixture_bytes("oddspapi", "nba_board.json")


def make(engine, settings, fake_api, clock, monkeypatch, **options):
    monkeypatch.setenv("ODDSPAPI_KEY", "op_test")
    cfg = settings.source("oddspapi")
    opts = {**cfg.options, "tournaments": {**cfg.options["tournaments"], "nba": 132}}
    opts.update(options)
    cfg = dataclasses.replace(cfg, options=opts)
    return OddsPapiSource(engine, cfg, transport=fake_api.transport, clock=clock)


@pytest.fixture
def papi(engine, settings, fake_api, clock, monkeypatch):
    return make(engine, settings, fake_api, clock, monkeypatch)


def snaps(engine):
    with engine.connect() as conn:
        return (
            conn.execute(select(odds_snapshots, books.c.key.label("book")).join(books))
            .mappings()
            .all()
        )


def test_board_request_and_mapped_moneyline(papi, engine, fake_api):
    fake_api.add(body=BOARD)
    result = papi.run("odds", sport="nba")
    assert result.status == "ok", result.message
    req = fake_api.requests[0]
    assert req.url.path == "/v4/odds-by-tournaments"
    assert dict(req.url.params) == {"tournamentIds": "132", "bookmaker": "pinnacle",
                                    "apiKey": "op_test"}  # fmt: skip
    rows = snaps(engine)
    assert sorted((r["outcome"], r["price_american"]) for r in rows) == [
        ("away", 155),
        ("home", -182),
    ]
    assert {r["book"] for r in rows} == {"pinnacle"}  # seeded sharp book
    assert papi.quota.status().used_effective == 1
    with engine.connect() as conn:
        params = conn.execute(select(raw_responses.c.params)).scalar()
    assert "apiKey" in params and params["apiKey"] == "***"


def test_unmapped_markets_are_kept_for_reparse(
    engine, settings, fake_api, clock, monkeypatch, caplog
):
    papi = make(engine, settings, fake_api, clock, monkeypatch)
    fake_api.add(body=BOARD)
    with caplog.at_level(logging.INFO):
        papi.run("odds", sport="nba")
    assert "unmapped market ids ['113', '117']" in caplog.text
    assert len(snaps(engine)) == 2

    # The user fills in the map from the markets catalog, then replays the stored response.
    markets = {"nba": {
        "111": {"market": "h2h", "outcomes": {"111": "home", "112": "away"}},
        "113": {"market": "spreads", "outcomes": {"113": "home", "114": "away"}},
        "117": {"market": "totals", "outcomes": {"117": "over", "118": "under"}},
    }}  # fmt: skip
    papi = make(engine, settings, fake_api, clock, monkeypatch, markets=markets)
    with engine.connect() as conn:
        raw_id = conn.execute(select(raw_responses.c.raw_id)).scalar()
    assert papi.reparse(raw_id) == (4, 6)
    got = {(r["outcome"], r["line"]): r for r in snaps(engine)}
    assert got[("home", -4.5)]["price_american"] == -110
    assert got[("over", 224.5)]["price_american"] == -115  # market-level handicap
    assert got[("under", 224.5)]["price_status"] == "suspended"
    assert len(fake_api.requests) == 1  # no second call


def test_unconfigured_league_is_skipped_without_a_call(papi, fake_api):
    result = papi.run("odds", sport="ncaab")
    assert result.status == "skipped" and "tournaments" in result.message
    assert fake_api.requests == []


def test_shape_change_fails_but_keeps_raw(papi, engine, fake_api):
    fake_api.add(body=BOARD.replace(b"participant1Name", b"homeName"))
    result = papi.run("odds", sport="nba")
    assert result.status == "failed" and "participant1Name" in result.message
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(raw_responses)).scalar() == 1


def test_catalog_listing(papi, fake_api, caplog):
    fake_api.add(body=fixture_bytes("oddspapi", "tournaments_basketball.json"))
    with caplog.at_level(logging.INFO):
        result = papi.run("tournaments", sport_id="11")
    assert result.status == "ok" and result.rows_seen == 2
    assert fake_api.requests[0].url.params["sportId"] == "11"
    assert "132\tNBA" in caplog.text
