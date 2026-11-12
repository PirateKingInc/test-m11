"""The Action review (owner 2026-11-12, betdata/action_review.py)."""

from datetime import UTC, datetime

from betdata import action_review as AR
from betdata.ledger import milestones, read_rows
from betdata.weekly import milestone_lines
from tests.test_action_records import _with_action

NOW = datetime(2026, 12, 1, 12, tzinfo=UTC)


def test_milestone_at_100_graded_once(tmp_path, monkeypatch):
    _with_action(tmp_path)  # 3 graded Action picks
    monkeypatch.setattr(AR, "REVIEW_N", 4)
    assert [m for m in milestones(tmp_path, NOW) if m["signal_name"] == AR.MILESTONE] == []
    monkeypatch.setattr(AR, "REVIEW_N", 3)
    got = [m for m in milestones(tmp_path, NOW) if m["signal_name"] == AR.MILESTONE]
    assert got == [{"signal_name": "action-card", "graded": "3",
                    "reached_at": "2026-12-01T12:00:00+00:00"}]
    assert not [m for m in milestones(tmp_path, NOW) if m["signal_name"] == AR.MILESTONE]
    assert len([r for r in read_rows(tmp_path / "milestones.csv")
                if r["signal_name"] == AR.MILESTONE]) == 1  # fmt: skip
    root = tmp_path / "root"
    (root / "ledger").mkdir(parents=True)
    _with_action(root / "ledger")
    (line,) = milestone_lines(got, root)
    assert line.startswith("Action review ready: 2-1-0, +0.33u over 3 graded Action picks")


def test_report_and_weekly_sections(tmp_path):
    (tmp_path / "ledger").mkdir()
    _with_action(tmp_path / "ledger")
    md = AR.report(tmp_path, NOW)
    assert "- Record: 2-1-0, +0.33u at 0.25u" in md
    assert "By reason: rebounds 2-0-0" in md and "By market:" in md
    assert "Same days (1): Action 2-1-0" in md and "Replay data not found." in md
    w = "\n".join(AR.weekly(tmp_path))
    assert w.startswith("## Action picks (not official)") and "2-1-0" in w


def test_stacks_count_same_player_nights():
    rows = [{"day": "d", "player": "Zubac", "result": "win"},
            {"day": "d", "player": "zubac", "result": "win"},
            {"day": "e", "player": "Zubac", "result": "win"},
            {"day": "e", "player": "Zubac", "result": "loss"}]
    assert AR.stacks(rows) == ["- Same-player stacks: 2 (all won 1, all lost 0, split 1)"]
