"""F's pre-registered verdict (owner 2026-11-12, betdata/f_verdict.py; MODEL.md "F verdict")."""

import csv
import math
from datetime import UTC, datetime

import pytest

from betdata import f_verdict as FV
from betdata.ledger import ANNOTATION_FIELDS, PICK_FIELDS, RESULT_FIELDS, milestones, read_rows

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)


def _w(path, fields, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def _ledger(out, specs, notes=()):
    """specs: (pick_id, created_at, result, fair_prob, odds, book_prob, clv, signal_name);
    results.csv rows are written in the order given (the grading order)."""
    picks, res = [], []
    for pid, made, result, p, odds, bp, clv, name in specs:
        picks.append({"pick_id": pid, "dedup_key": pid, "created_at": made, "sport": "nba",
                      "game_id": "1", "game": "A @ B", "tip": made, "market": "assists",
                      "player": pid, "side": "over", "line": "4.5", "odds": odds,
                      "book": "draftkings", "signal": "snipe", "signal_name": name,
                      "fair_prob": p, "book_prob": bp})
        if result:
            res.append({"pick_id": pid, "result": result, "clv_consensus": clv,
                        "profit": {"win": "0.9091", "loss": "-1.0"}.get(result, "0")})
    # picks.csv in creation order; results.csv in the given (grading) order
    _w(out / "picks.csv", PICK_FIELDS, sorted(picks, key=lambda r: r["created_at"]))
    _w(out / "results.csv", RESULT_FIELDS, res)
    _w(out / "annotations.csv", ANNOTATION_FIELDS, list(notes))


def test_window_is_deterministic_and_excludes_voids_legacy_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(FV, "WINDOW", 4)
    specs = [
        ("before", "2026-11-04T04:59:00+00:00", "win", "0.6", "-110", "0.5", "0.01", "F"),
        ("a", "2026-11-04T05:00:00+00:00", "loss", "0.6", "-110", "0.5", "0.01", "F"),
        ("void", "2026-11-04T06:00:00+00:00", "void", "0.6", "-110", "0.5", "0.01", "F"),
        ("cnba", "2026-11-04T06:00:00+00:00", "win", "0.6", "-110", "0.5", "0.01", "C-NBA"),
        ("dup", "2026-11-04T07:00:00+00:00", "win", "0.6", "-110", "0.5", "0.01", "F"),
        ("legacy", "2026-11-04T07:00:00+00:00", "win", "0.6", "-110", "0.5", "0.01", "F"),
        ("c", "2026-11-05T10:00:00+00:00", "push", "0.6", "-110", "0.5", "0.01", "F"),
        ("pending", "2026-11-04T08:00:00+00:00", "", "0.6", "-110", "0.5", "", "F"),
        ("b", "2026-11-04T09:00:00+00:00", "win", "0.6", "-110", "0.5", "0.01", "F"),
        ("d", "2026-11-04T09:30:00+00:00", "win", "0.6", "-110", "0.5", "0.01", "F"),
        ("e", "2026-11-04T09:40:00+00:00", "loss", "0.6", "-110", "0.5", "0.01", "F"),
    ]
    notes = [{"key": "dup:duplicate", "pick_id": "dup", "field": "duplicate", "value": "true"},
             {"key": "legacy:signal_name", "pick_id": "legacy", "field": "signal_name",
              "value": "m16-legacy"}]  # fmt: skip
    _ledger(tmp_path, specs, notes)
    # by hand: grading order a, (void out), (cnba out), (dup out), (legacy out), c (push
    # counts), (pending out), b, d -> the first 4; "before" is 04:59 UTC = Nov 3 11:59 PM ET
    assert [r["pick_id"] for r in FV.window(tmp_path)] == ["a", "c", "b", "d"]
    assert [r["pick_id"] for r in FV.window(tmp_path)] == ["a", "c", "b", "d"]  # same again
    assert [r["pick_id"] for r in FV.graded_since(tmp_path)] == ["a", "c", "b", "d", "e"]


def _fixture(tmp_path, n=200):
    """n F picks: model p, close p (book_prob + clv) and results chosen so every check can be
    computed by hand."""
    specs = []
    for i in range(n):
        p = "0.6" if i % 2 == 0 else "0.5"
        res = "win" if i % 4 in (0, 1) else "loss"  # half win
        specs.append((f"p{i:03d}", f"2026-11-05T{(i // 60) % 24:02d}:{i % 60:02d}:00+00:00",
                      res, p, "100", "0.5", "-0.02", "F"))
    _ledger(tmp_path, specs)


def test_three_checks_reproduce_model_md_by_hand(tmp_path):
    from betdata.checks import ECE_MAX

    _fixture(tmp_path)
    res = FV.judge(FV.window(tmp_path))
    c = res.checks
    # p = 0.6 on half the picks (they win half), 0.5 on the other half (win half):
    # bins 0.55-0.60 is [0.55, 0.60): 0.6 falls in 0.60-0.70; ECE = .5*|.6-.5| + .5*|.5-.5|
    assert c.calibration.value == pytest.approx(0.05)
    gap = [(0.6 if i % 2 == 0 else 0.5) - (1 if i % 4 in (0, 1) else 0) for i in range(200)]
    m = sum(gap) / 200
    sd = math.sqrt(sum((x - m) ** 2 for x in gap) / 199)
    assert c.calibration.low == pytest.approx(m - 1.96 * sd / math.sqrt(200))
    assert c.calibration.passed is (c.calibration.low <= 0 and 0.05 <= ECE_MAX)
    # close = 0.5 - 0.02 = 0.48 for every pick; EV = 0.48 * 2.0 - 1 = -0.04 exactly
    assert c.ev.value == pytest.approx(-0.04) and c.ev.passed is False
    # Brier(model) - Brier(close) per pick, averaged
    d = [((0.6 if i % 2 == 0 else 0.5) - y) ** 2 - (0.48 - y) ** 2
         for i in range(200) for y in [1 if i % 4 in (0, 1) else 0]]
    assert c.brier.value == pytest.approx(sum(d) / 200)
    assert res.verdict == "does not work" and "EV at close" in res.failed


def test_milestone_once_and_ntfy_line(tmp_path):
    _fixture(tmp_path, 199)
    assert [m for m in milestones(tmp_path, NOW) if m["signal_name"] == FV.MILESTONE] == []
    _fixture(tmp_path, 210)
    got = [m for m in milestones(tmp_path, NOW) if m["signal_name"] == FV.MILESTONE]
    assert got == [{"signal_name": "F:verdict-nov4", "graded": "200",
                    "reached_at": "2026-11-12T12:00:00+00:00"}]
    assert not [m for m in milestones(tmp_path, NOW) if m["signal_name"] == FV.MILESTONE]
    assert FV.ntfy_line(tmp_path) == ("F verdict (next 200 from Nov 4): does not work — see "
                                      "reports/nba/f_verdict.md")
    rows = [r for r in read_rows(tmp_path / "milestones.csv") if r["signal_name"] == FV.MILESTONE]
    assert len(rows) == 1


def test_report_has_the_verdict_and_the_labelled_views(tmp_path):
    (tmp_path / "ledger").mkdir()
    _fixture(tmp_path / "ledger", 220)
    path, res = FV.write(tmp_path, NOW)
    md = path.read_text()
    assert "**Verdict: F does not work**" in md
    assert "NOT the verdict: every F pick graded since Nov 4" in md
    assert "NOT the verdict: the window, one pick per player per game" in md
    assert "| 50-55% |" in md and "| 60-65% |" in md


def test_dedup_keeps_highest_probability_per_player_game():
    rows = [{"game_id": "1", "player": "A", "fair_prob": "0.55"},
            {"game_id": "1", "player": "a", "fair_prob": "0.60"},
            {"game_id": "2", "player": "A", "fair_prob": "0.50"}]
    assert [r["fair_prob"] for r in FV.dedup(rows)] == ["0.60", "0.50"]
