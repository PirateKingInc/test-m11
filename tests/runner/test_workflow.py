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
    assert [s["cron"] for s in on["schedule"]] == [window.CRON]
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
