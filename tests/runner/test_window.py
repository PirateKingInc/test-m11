"""Verification (c): the hourly runner works 7am-10pm Eastern, 16 runs a day, through the
daylight-saving changes (Nov 1, 2026 and Mar 14, 2027)."""

from datetime import UTC, date, datetime

import pytest

from betdata.runner import window


@pytest.mark.parametrize("day", [date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2),
                                 date(2027, 3, 13), date(2027, 3, 14), date(2027, 3, 15)])
def test_runs_exactly_7am_to_10pm_eastern(day):
    runs = window.runs_on(day)
    assert [t.hour for t in runs] == list(range(7, 23))
    assert all(t.minute == 0 for t in runs)


def test_gate_and_late_starts():
    # 10:59pm EDT (02:59 UTC) belongs to the 10pm run; 11pm EDT is outside.
    assert window.in_window(datetime(2026, 10, 20, 2, 59, tzinfo=UTC))
    assert not window.in_window(datetime(2026, 10, 20, 3, 0, tzinfo=UTC))
    # In EST 03:00 UTC is 10pm (inside) and 11:00 UTC is 6am (outside).
    assert window.in_window(datetime(2026, 11, 3, 3, 10, tzinfo=UTC))
    assert not window.in_window(datetime(2026, 11, 3, 11, 30, tzinfo=UTC))
    # A run GitHub starts 25 minutes late still belongs to its hour.
    assert window.slot(datetime(2026, 10, 20, 15, 25, tzinfo=UTC)) == datetime(
        2026, 10, 20, 15, tzinfo=UTC)  # fmt: skip


def test_gate_script_output(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    window.main(["w", "2026-10-20T03:00:00+00:00"])
    window.main(["w", "2026-10-20T16:00:00+00:00"])
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    window.main(["w", "2026-10-20T03:00:00+00:00"])  # manual runs always run
    assert out.read_text().split() == ["run=false", "run=true", "run=true"]
