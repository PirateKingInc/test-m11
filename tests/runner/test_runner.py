"""M13 verification: the workflow schedule, jobs due per hour, a dummy second sport on the page
and in grading (g), lost state (e) and a forced failure (f)."""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from sqlalchemy import select

from betdata.config import PROJECT_ROOT
from betdata.db.schema import runner_runs
from betdata.page.build import build_site, mark_failed
from betdata.runner import window
from betdata.runner.hourly import due, run_hourly, run_job
from betdata.scheduler.jobs import JobDef
from betdata.sports import active_sports, load_entries
from tests.runner import dummy_sport

NOW = datetime(2026, 10, 20, 16, 5, tzinfo=UTC)  # 12:05 EDT


# --- (c) schedule and DST -----------------------------------------------------------------


def test_workflow_cron_matches_the_window():
    wf = yaml.safe_load((PROJECT_ROOT / ".github" / "workflows" / "hourly.yml").read_text())
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` as True
    crons = [s["cron"] for s in on["schedule"]]
    assert crons == [window.CRON]
    assert "workflow_dispatch" in on
    hours = re.match(r"0 (\S+) \* \* \*", window.CRON).group(1)
    listed = set()
    for part in hours.split(","):
        lo, _, hi = part.partition("-")
        listed |= set(range(int(lo), int(hi or lo) + 1))
    assert listed == set(window.CRON_UTC_HOURS)
    assert wf["concurrency"]["cancel-in-progress"] is False  # runs never overlap


def test_jobs_due_by_eastern_hour():
    hourly = JobDef("h", "nflverse", "schedules", "0 7-22 * * *")
    morning = JobDef("m", "nflverse", "pbp", "0 7 * * *")
    seven_edt = datetime(2026, 10, 20, 11, tzinfo=UTC)
    seven_est = datetime(2026, 11, 3, 12, tzinfo=UTC)
    assert due(hourly, seven_edt) and due(morning, seven_edt)
    assert due(morning, seven_est) and not due(morning, seven_est + timedelta(hours=1))
    assert due(hourly, seven_est + timedelta(hours=15))  # 10pm EST
    assert not due(hourly, seven_est + timedelta(hours=16))  # 11pm


def test_job_without_key_is_skipped_not_failed(engine, settings, monkeypatch):
    monkeypatch.delenv("SHARPAPI_KEY")
    job = JobDef("s", "sharpapi", "odds", "0 7-22 * * *", params={"sport": "nfl"})
    out = run_job(engine, settings, {}, job, NOW, None)
    assert (out["status"], out["message"]) == ("skipped", "SHARPAPI_KEY not set")


# --- (g) a second sport through shared code ------------------------------------------------


def _dummy_entries(tmp_path):
    cfg = {"sports": [
        {"key": "ncaaf", "name": "DummyBall", "status": "active",
         "plugin": "tests.runner.dummy_sport:DummySport", "jobs": []},
        {"key": "nba", "name": "NBA", "status": "coming_soon"},
    ]}  # fmt: skip
    path = tmp_path / "sports.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return load_entries(path)


def test_dummy_sport_on_page_and_in_grading(engine, settings, tmp_path):
    dummy_sport.CALLS.clear()
    entries = _dummy_entries(tmp_path)
    active = active_sports(entries)
    report = run_hourly(engine, settings, {}, active, NOW)
    assert report.status == "ok" and report.rebuilt  # empty database: bootstrapped first
    assert dummy_sport.CALLS == ["bootstrap", "predict", "grade"]
    assert report.sports["ncaaf"]["graded"]["inserted"] == 1
    with engine.connect() as conn:
        build_site(conn, settings, entries, active, tmp_path / "site", NOW)
    html = (tmp_path / "site" / "index.html").read_text()
    assert "DummyBall" in html and "AWAYX @ HOMEX" in html
    assert "Spread: HOMEX" in html  # flagged at its own threshold
    assert "stub factor" in html and "Player Zed" in html
    assert "7-3-0" in html and "dummy:v1" in html  # season record from its grading
    assert "NBA <small>coming soon</small>" in html
    # A second run: history is there now, so no rebuild.
    again = run_hourly(engine, settings, {}, active, NOW + timedelta(hours=1))
    assert not again.rebuilt and dummy_sport.CALLS.count("bootstrap") == 1


# --- (e) lost state and (f) a failed run ----------------------------------------------------


def test_lost_state_rebuilds_and_the_page_says_so(engine, settings, tmp_path):
    """An empty database (the saved state was lost) is rebuilt by the sport's bootstrap; the
    run records it and the page shows the notice."""
    entries = _dummy_entries(tmp_path)
    active = active_sports(entries)
    report = run_hourly(engine, settings, {}, active, NOW)
    assert report.rebuilt
    with engine.connect() as conn:
        status = build_site(conn, settings, entries, active, tmp_path / "site", NOW)
        row = conn.execute(select(runner_runs)).mappings().one()
    assert row["rebuilt"] and row["status"] == "ok"
    assert status["rebuilt_at"] is not None
    assert "were lost" not in status  # the wording lives in the page script
    html = (tmp_path / "site" / "index.html").read_text()
    assert "rebuilt from free sources" in html and '"rebuilt_at": "2026-10-20' in html


def test_failed_run_keeps_last_good_page_with_warning(engine, settings, tmp_path):
    entries = _dummy_entries(tmp_path)
    active = active_sports(entries)
    run_hourly(engine, settings, {}, active, NOW)
    good = tmp_path / "good"
    with engine.connect() as conn:
        build_site(conn, settings, entries, active, good, NOW)
    with pytest.raises(RuntimeError, match="forced failure"):
        run_hourly(engine, settings, {}, active, NOW + timedelta(hours=1), fail="forced failure")
    with engine.connect() as conn:
        rows = conn.execute(select(runner_runs.c.status).order_by(runner_runs.c.run_id)).all()
    assert [r.status for r in rows] == ["ok", "failed"]

    site = tmp_path / "site"
    mark_failed(site, good, NOW + timedelta(hours=1, minutes=2), "RuntimeError: forced failure",
                "https://example.invalid/run/1")  # fmt: skip
    status = json.loads((site / "status.json").read_text())
    assert status["last_run"]["status"] == "failed"
    assert status["last_ok_at"].startswith("2026-10-20T16:05")  # the good data is still shown
    html = (site / "index.html").read_text()
    assert "AWAYX @ HOMEX" in html  # the last good games
    embedded = re.search(r'<script id="status" type="application/json">(.*?)</script>', html)
    assert json.loads(embedded.group(1))["last_run"]["message"] == "RuntimeError: forced failure"
    assert html.count('<script id="status"') == 1
    # With no good page at all, a minimal page still says what failed.
    mark_failed(tmp_path / "bare", None, NOW, "boom")
    assert json.loads((tmp_path / "bare" / "status.json").read_text())["last_run"]["message"] \
        == "boom"




def test_nfl_bootstrap_loads_history_from_nflverse(engine, settings, fake_api, clock, tmp_path,
                                                   monkeypatch):  # fmt: skip
    """The NFL rebuild after lost state: every nflverse dataset per season, then derive (here
    one season, from fixture files, through the fake transport)."""
    from sqlalchemy import func

    from betdata.db.schema import data_files, games, roster_weeks, team_game_stats
    from betdata.sports import nfl as nfl_mod
    from tests.conftest import fixture_bytes

    monkeypatch.setenv("BETDATA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(nfl_mod, "FIRST_HISTORY_SEASON", 2024)
    for name in ("games_sample.csv", "stats_team_week_2024.csv", "snap_counts_2024.csv",
                 "injuries_2026.csv", "roster_weekly_2024.csv", "play_by_play_2025.parquet"):
        fake_api.add(body=fixture_bytes("nflverse", name))
    [sport] = active_sports()
    sport.bootstrap(engine, datetime(2024, 10, 15, tzinfo=UTC),
                    {"transport": fake_api.transport, "sleep": lambda s: None, "clock": clock})
    paths = [r.url.path for r in fake_api.requests]
    assert paths[0].endswith("/schedules/games.csv")
    assert [p.rsplit("/", 1)[1] for p in paths[1:]] == [
        "stats_team_week_2024.csv", "snap_counts_2024.csv", "injuries_2024.csv",
        "roster_weekly_2024.csv", "play_by_play_2024.parquet"]  # fmt: skip
    with engine.connect() as conn:
        for table in (games, team_game_stats, roster_weeks, data_files):
            assert conn.execute(select(func.count()).select_from(table)).scalar() > 0, table
