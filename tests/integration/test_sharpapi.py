"""M6: SharpAPI odds, pagination, suspension, rate spacing, closing-line view."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

from betdata.db.schema import books, games, odds_snapshots, raw_responses
from betdata.sources.sharpapi import SharpApiSource
from tests.conftest import fixture_bytes

PAGE1 = fixture_bytes("sharpapi", "nba_odds_page1.json")
PAGE2 = fixture_bytes("sharpapi", "nba_odds_page2.json")
RATE = {"x-ratelimit-limit": "12", "x-ratelimit-remaining": "11"}


class FakeTime:
    """Monotonic clock that sleeping advances, with a log of the sleeps."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.fixture
def fake_time():
    return FakeTime()


@pytest.fixture
def sharp(engine, settings, fake_api, clock, monkeypatch, fake_time):
    monkeypatch.setenv("SHARPAPI_KEY", "sk_test")
    return SharpApiSource(
        engine, settings.source("sharpapi"), transport=fake_api.transport,
        sleep=fake_time.sleep, clock=clock, monotonic=fake_time.monotonic,
    )  # fmt: skip


def board(fake_api, *pages):
    for p in pages:
        fake_api.add(body=p, headers=RATE)


def rows(engine):
    with engine.connect() as conn:
        return conn.execute(
            select(odds_snapshots, books.c.key.label("book")).join(books)
            .order_by(odds_snapshots.c.snapshot_id)
        ).mappings().all()  # fmt: skip


def test_request_shape_and_pagination(sharp, fake_api, fake_time):
    board(fake_api, PAGE1, PAGE2)
    result = sharp.run("odds", sport="nba")
    assert result.status == "ok", result.message
    r1, r2 = fake_api.requests
    assert r1.url.path == "/api/v1/odds" and r1.headers["x-api-key"] == "sk_test"
    q = dict(r1.url.params)
    assert q == {
        "league": "nba",
        "sportsbook": "draftkings,fanduel",
        "limit": "500",
        "offset": "0",
        "market": "moneyline,point_spread,total_points",
        "live": "false",
        "is_main_line": "true",
    }
    assert r2.url.params["offset"] == "500"
    assert fake_time.sleeps == [10.0]  # second page waited out the 10 s spacing


def test_selections_loaded_props_and_segments_skipped(sharp, engine, fake_api):
    board(fake_api, PAGE1, PAGE2)
    result = sharp.run("odds", sport="nba")
    assert result.rows_inserted == 10 and result.rows_seen == 10
    got = {(r["book"], r["outcome"], r["line"]): r for r in rows(engine)}
    assert got[("draftkings", "home", -4.5)]["price_american"] == -110
    assert got[("fanduel", "home", -4.5)]["price_american"] == -112  # "spread", no team_side
    assert got[("draftkings", "over", 224.5)]["price_american"] == -108
    assert got[("fanduel", "away", None)]["price_status"] == "suspended"
    assert not [r for r in rows(engine) if r["line"] in (-2.5, 27.5)]  # 1st half, prop
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(games)).scalar() == 1
        assert conn.execute(select(func.count()).select_from(raw_responses)).scalar() == 2


def test_changes_suspension_and_removal(sharp, engine, fake_api, clock):
    board(fake_api, PAGE1, PAGE2)
    sharp.run("odds", sport="nba")
    clock.advance(minutes=10)
    p1 = json.loads(PAGE1)
    p1["data"] = [d for d in p1["data"] if d["market_type"] != "total_points"]  # DK pulls total
    for d in p1["data"]:
        if d["id"] == "8":
            d["is_active"] = True  # FanDuel reopens the away moneyline
        if d["id"] == "1":
            d["odds_american"] = -190  # DraftKings moves
    board(fake_api, json.dumps(p1).encode(), PAGE2)
    result = sharp.run("odds", sport="nba")
    new = [r for r in rows(engine) if r["run_id"] == result.run_id]
    assert sorted((r["book"], r["outcome"], r["price_status"]) for r in new) == [
        ("draftkings", "home", "active"),  # -180 -> -190
        ("draftkings", "over", "removed"),
        ("draftkings", "under", "removed"),
        ("fanduel", "away", "active"),  # suspended -> active
    ]


def test_replaying_one_page_never_flags_removals(sharp, engine, fake_api, clock):
    board(fake_api, PAGE1, PAGE2)
    sharp.run("odds", sport="nba")
    clock.advance(minutes=10)
    board(fake_api, PAGE1, PAGE2)
    sharp.run("odds", sport="nba")
    with engine.connect() as conn:
        page2_raw = conn.execute(select(func.max(raw_responses.c.raw_id))).scalar()
    sharp.reparse(page2_raw)  # page 2 alone lacks page 1's selections
    assert not [r for r in rows(engine) if r["price_status"] == "removed"]


def test_closing_line_view(sharp, engine, fake_api, clock):
    # clock starts 2026-10-15; move to the evening of the game (tip 2026-11-21 00:30Z)
    clock.now = clock.now.replace(year=2026, month=11, day=20, hour=23, minute=0)
    board(fake_api, PAGE1, PAGE2)
    sharp.run("odds", sport="nba")
    clock.advance(minutes=50)  # 23:50Z: DK moves to -195
    p1 = json.loads(PAGE1)
    p1["data"][0]["odds_american"] = -195
    board(fake_api, json.dumps(p1).encode(), PAGE2)
    sharp.run("odds", sport="nba")
    clock.advance(hours=1)  # 00:50Z, after tip: a later move must not count as the close
    p1["data"][0]["odds_american"] = -400
    board(fake_api, json.dumps(p1).encode(), PAGE2)
    sharp.run("odds", sport="nba")
    with engine.connect() as conn:
        close = conn.execute(text(
            "SELECT c.price_american, c.closing_basis, c.game_outcome FROM v_closing_lines c "
            "JOIN books b ON b.book_id = c.book_id JOIN markets m ON m.market_id = c.market_id "
            "WHERE b.key='draftkings' AND m.key='h2h' AND c.outcome='home'"
        )).one()  # fmt: skip
    assert close == (-195, "last_poll_before_start", "home")


def test_missing_key_and_unknown_league(engine, settings, fake_api, monkeypatch):
    monkeypatch.delenv("SHARPAPI_KEY", raising=False)
    src = SharpApiSource(engine, settings.source("sharpapi"), transport=fake_api.transport)
    assert "SHARPAPI_KEY" in src.run("odds", sport="nba").message
    monkeypatch.setenv("SHARPAPI_KEY", "k")
    src = SharpApiSource(engine, settings.source("sharpapi"), transport=fake_api.transport)
    assert "no league configured" in src.run("odds", sport="mlb").message
    assert fake_api.requests == []


def test_runaway_quota_guard(sharp, engine, fake_api):
    sharp.quota.reconcile(remaining=None, used=None)
    status = sharp.quota.status()
    assert status.limit == 3000 and status.period_end - status.period_start == timedelta(days=1)


def _free_rows(n, start, line=-4.5, main=True, event="e1"):
    """n spread rows in the free tier's shape (no pagination metadata)."""
    return [{"id": f"{event}-{i}", "sportsbook": "draftkings", "event_id": event,
             "home_team": "Boston Celtics", "away_team": "New York Knicks",
             "market_type": "point_spread", "team_side": "home", "selection": "Boston Celtics",
             "line": line, "odds_american": -110, "event_start_time": start,
             "is_main_line": main} for i in range(n)]  # fmt: skip


def test_free_tier_pages_by_row_count_and_horizon(sharp, fake_api, clock):
    # M16 (REVIEW M16-3): 200 rows and no metadata means "maybe more"; stop at a short page...
    soon = (clock.now + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    board(fake_api, json.dumps({"data": _free_rows(200, soon)}).encode(),
          json.dumps({"data": _free_rows(3, soon)}).encode())  # fmt: skip
    assert sharp.run("odds", sport="nba").status == "ok"
    assert [r.url.params["offset"] for r in fake_api.requests] == ["0", "200"]
    # ...or once a full page reaches past the horizon (events are sorted by start).
    fake_api.requests.clear()
    later = (clock.now + timedelta(days=30)).isoformat().replace("+00:00", "Z")
    board(fake_api, json.dumps({"data": _free_rows(200, later)}).encode())
    assert sharp.run("odds", sport="nba").status == "ok"
    assert len(fake_api.requests) == 1


def test_alternate_lines_are_never_stored(sharp, engine, fake_api, clock):
    # M16 (REVIEW M16-3): an alternate spread stored as a poll became the page's market line.
    soon = (clock.now + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    data = _free_rows(1, soon, line=-4.5) + _free_rows(1, soon, line=14.5, main=False)
    data[1]["id"] = "alt"
    board(fake_api, json.dumps({"data": data}).encode())
    sharp.run("odds", sport="nba")
    assert [r["line"] for r in rows(engine)] == [-4.5]
