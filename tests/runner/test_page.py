"""The page (Part D) built from a stub sport: tabs from config, game cards, line movement over
today's runs, season record, warnings; and the failure page."""

import json
import re
from datetime import UTC, datetime, timedelta

import yaml
from sqlalchemy import insert

from betdata.db.schema import runner_runs
from betdata.page.build import FOOTER, build_site, et, mark_failed, spread_txt, stale
from betdata.sports import active_sports, load_entries

NOW = datetime(2026, 10, 20, 16, 5, tzinfo=UTC)  # 12:05 EDT


def entries(tmp_path):
    cfg = {"sports": [
        {"key": "ncaaf", "name": "DummyBall", "status": "active",
         "plugin": "tests.runner.dummy_sport:DummySport"},
        {"key": "nba", "name": "NBA", "status": "coming_soon"},
    ]}  # fmt: skip
    path = tmp_path / "sports.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return load_entries(path)


def runs(engine, *rows):
    with engine.begin() as conn:
        for at, status, rebuilt in rows:
            conn.execute(insert(runner_runs).values(
                slot_at=at.replace(minute=0), started_at=at, finished_at=at, trigger="schedule",
                status=status, rebuilt=rebuilt, details={"jobs": [
                    {"sport": "ncaaf", "source": "sharpapi", "job": "odds",
                     "status": "skipped", "message": "SHARPAPI_KEY not set"}]}))  # fmt: skip


def test_page(engine, settings, tmp_path):
    ens = entries(tmp_path)
    runs(engine, (NOW - timedelta(hours=1), "ok", True), (NOW, "ok", False))
    with engine.connect() as conn:
        status = build_site(conn, settings, ens, active_sports(ens), tmp_path / "s", NOW)
    html = (tmp_path / "s" / "index.html").read_text()
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert "DummyBall" in html and "NBA <small>coming soon</small>" in html
    assert "AWAYX @ HOMEX" in html and "Spread: HOMEX" in html
    assert "stub factor" in html and "Player Zed" in html
    assert "Line movement today (2 runs)" in html  # one row per successful run today
    assert "7-3-0" in html and "+36.0%" in html and "dummy:v1" in html
    assert "SharpAPI odds: skipped — SHARPAPI_KEY not set" in html
    assert FOOTER in html and "Last updated" in html
    assert status["last_ok_at"].startswith("2026-10-20T16:05")
    assert status["rebuilt_at"].startswith("2026-10-20T15:05")
    assert not stale(status, NOW)
    assert json.loads((tmp_path / "s" / "status.json").read_text()) == status
    assert "Weather data by" in html and "open-meteo.com" in html  # CC BY 4.0 attribution


def test_rebuilt_notice_clears_after_48_hours(engine, settings, tmp_path):
    ens = entries(tmp_path)
    rebuilt = NOW - timedelta(hours=47)
    runs(engine, (rebuilt, "ok", True), (NOW, "ok", False))
    with engine.connect() as conn:
        assert build_site(conn, settings, ens, active_sports(ens), tmp_path / "a", NOW)[
            "rebuilt_at"].startswith("2026-10-18T17:05")
        later = NOW + timedelta(hours=1, minutes=1)  # 48 h 1 min after the rebuild
        assert build_site(conn, settings, ens, active_sports(ens), tmp_path / "b",
                          later)["rebuilt_at"] is None  # fmt: skip
    assert "<48*36e5" in (tmp_path / "b" / "index.html").read_text()  # and in the browser


def test_moneyline_flags_only_when_the_final_test_made_money(engine, settings, tmp_path,
                                                            monkeypatch):  # fmt: skip
    from tests.runner.dummy_sport import DummySport

    ens = entries(tmp_path)
    runs(engine, (NOW, "ok", False))
    for roi, flagged in ((None, False), (-0.092, False), (0.03, True)):
        monkeypatch.setattr(DummySport, "ML_ROI", roi)
        with engine.connect() as conn:
            build_site(conn, settings, ens, active_sports(ens), tmp_path / "s", NOW)
        html = (tmp_path / "s" / "index.html").read_text()
        assert ("Moneyline: HOMEX" in html) == flagged, roi
        assert "Spread: HOMEX" in html  # spread flags unchanged
        assert "+5.0 pp" in html  # the edge is shown either way
        assert ("information only" in html) == (not flagged)
    monkeypatch.setattr(DummySport, "ML_ROI", -0.092)
    with engine.connect() as conn:
        build_site(conn, settings, ens, active_sports(ens), tmp_path / "s", NOW)
    assert "(it was -9.2%)" in (tmp_path / "s" / "index.html").read_text()


def test_nfl_moneyline_flags_are_off():
    nfl = next(s for s in active_sports() if s.key == "nfl")
    assert nfl.moneyline_final_roi() == -0.092  # MODEL.md: NFL moneyline final test


def test_failure_page_keeps_the_last_good_one(engine, settings, tmp_path):
    ens = entries(tmp_path)
    runs(engine, (NOW, "ok", False))
    good = tmp_path / "good"
    with engine.connect() as conn:
        build_site(conn, settings, ens, active_sports(ens), good, NOW)
    site = tmp_path / "site"
    mark_failed(site, good, NOW + timedelta(hours=1), "RuntimeError: forced failure", "u")
    status = json.loads((site / "status.json").read_text())
    assert status["last_run"]["status"] == "failed"
    assert status["last_ok_at"].startswith("2026-10-20T16:05")
    html = (site / "index.html").read_text()
    assert "AWAYX @ HOMEX" in html
    embedded = re.search(r'<script id="status" type="application/json">(.*?)</script>', html)
    assert json.loads(embedded.group(1))["last_run"]["message"] == "RuntimeError: forced failure"
    assert html.count('<script id="status"') == 1
    mark_failed(tmp_path / "bare", None, NOW, "boom")  # no good page yet: a minimal one
    assert json.loads((tmp_path / "bare" / "status.json").read_text())["last_run"]["message"] \
        == "boom"


def test_formatting():
    assert et(datetime(2026, 11, 1, 18, tzinfo=UTC)) == "Sun Nov 1, 1:00 PM ET"  # EST
    assert et(datetime(2026, 10, 25, 17, tzinfo=UTC)) == "Sun Oct 25, 1:00 PM ET"  # EDT
    assert spread_txt("KC", "BUF", -3.5) == "KC -3.5"
    assert spread_txt("KC", "BUF", 2.0) == "BUF -2"
    assert spread_txt("KC", "BUF", 0.0) == "pick'em"
    assert stale({"last_ok_at": NOW.isoformat()}, NOW + timedelta(hours=2, minutes=1))
    assert not stale({"last_ok_at": NOW.isoformat()}, NOW + timedelta(hours=1, minutes=59))
