"""The workflow file matches the run window, and the CLI drives a run end to end."""

import re
from datetime import UTC, datetime

import yaml
from typer.testing import CliRunner

from betdata.config import PROJECT_ROOT
from betdata.runner import window


def test_workflow_cron_matches_the_window():
    wf = yaml.safe_load((PROJECT_ROOT / ".github" / "workflows" / "hourly.yml").read_text())
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` as True
    assert [s["cron"] for s in on["schedule"]] == [window.CRON, window.BACKUP_CRON]
    assert window.BACKUP_CRON.split()[1:] == window.CRON.split()[1:]  # same hours
    assert "workflow_dispatch" in on
    hours = re.match(r"0 (\S+) \* \* \*", window.CRON).group(1)
    listed = set()
    for part in hours.split(","):
        lo, _, hi = part.partition("-")
        listed |= set(range(int(lo), int(hi or lo) + 1))
    assert listed == set(window.CRON_UTC_HOURS)
    assert wf["concurrency"]["cancel-in-progress"] is False  # runs never overlap
    run = wf["jobs"]["run"]
    assert run["needs"] == "gate" and "outputs.run == 'true'" in run["if"]
    for secret in ("ODDS_API_KEY", "SHARPAPI_KEY", "ODDSPAPI_KEY", "CFBD_API_KEY"):
        assert "secrets." in run["env"][secret]  # keys only from GitHub Secrets


def test_cli_failed_run_then_failure_page(engine, tmp_path, monkeypatch):
    from betdata.cli import app
    from betdata.config import get_settings

    monkeypatch.setenv("BETDATA_DB_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("BETDATA_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        site = tmp_path / "site"
        out = runner.invoke(app, ["runner", "run", "--site", str(site), "--fail", "forced",
                                  "--at", datetime(2026, 10, 20, 16, tzinfo=UTC).isoformat()])
        assert out.exit_code == 1 and "run failed: RuntimeError: forced" in out.output
        out = runner.invoke(app, ["runner", "failed-page", "--site", str(site), "--message",
                                  "RuntimeError: forced"])  # fmt: skip
        assert out.exit_code == 0, out.output
        assert '"status": "failed"' in (site / "status.json").read_text()
        assert (site / "index.html").exists()
    finally:
        get_settings.cache_clear()


def test_backup_trigger_skips_a_slot_that_already_ran(engine, tmp_path, monkeypatch):
    from sqlalchemy import insert

    from betdata.cli import app
    from betdata.config import get_settings
    from betdata.db.schema import runner_runs
    from betdata.runner.hourly import slot_done

    t = datetime(2026, 10, 20, 16, 2, tzinfo=UTC)
    assert not slot_done(engine, t)
    with engine.begin() as conn:
        conn.execute(insert(runner_runs).values(
            slot_at=window.slot(t), started_at=t, finished_at=t, trigger="schedule",
            status="ok", rebuilt=False))  # fmt: skip
    assert slot_done(engine, t.replace(minute=31))  # the :30 backup in the same hour
    assert not slot_done(engine, t.replace(hour=17))
    monkeypatch.setenv("BETDATA_DB_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("BETDATA_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    try:
        out = CliRunner().invoke(app, ["runner", "run", "--site", str(tmp_path / "site"),
                                       "--trigger", "schedule",
                                       "--at", t.replace(minute=31).isoformat()])  # fmt: skip
        assert out.exit_code == 0 and "slot already done" in out.output
        assert not (tmp_path / "site").exists()  # nothing rebuilt or republished
    finally:
        get_settings.cache_clear()


def test_clock_waits_to_the_next_hour_and_dispatches_inside_the_window(monkeypatch, tmp_path):
    slept = []
    t = datetime(2026, 10, 20, 14, 59, 30, tzinfo=UTC)
    assert window.wait_for_next_slot(t, slept.append) == datetime(2026, 10, 20, 15, tzinfo=UTC)
    assert slept == [35.0]  # 30 s to the hour, plus 5
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("RUN_TRIGGER", "clock")  # dispatched by the clock: gated like cron
    window.main(["w", "2026-10-20T03:00:00+00:00"])  # 11pm EDT
    window.main(["w", "2026-10-20T15:00:00+00:00"])  # 11am EDT
    monkeypatch.setenv("RUN_TRIGGER", "")  # a person pressing "Run workflow": always runs
    window.main(["w", "2026-10-20T03:00:00+00:00"])
    assert out.read_text().split() == ["run=false", "run=true", "run=true"]
    clock = yaml.safe_load((PROJECT_ROOT / ".github" / "workflows" / "clock.yml").read_text())
    assert clock["concurrency"]["cancel-in-progress"] is True  # one clock at a time
    steps = clock["jobs"]["tick"]["steps"]
    assert any("hourly.yml" in s.get("run", "") and "trigger=clock" in s["run"] for s in steps)
    assert any("clock.yml" in s.get("run", "") and s.get("if") == "always()" for s in steps)


def test_clock_and_cron_share_the_slot_dedupe(engine):
    from sqlalchemy import insert

    from betdata.db.schema import runner_runs
    from betdata.runner.hourly import slot_done

    t = datetime(2026, 10, 20, 15, 1, tzinfo=UTC)
    with engine.begin() as conn:
        conn.execute(insert(runner_runs).values(
            slot_at=window.slot(t), started_at=t, trigger="clock", status="ok", rebuilt=False))
    assert slot_done(engine, t.replace(minute=30))  # the cron backup then skips
