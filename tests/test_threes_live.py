"""F threes live (owner 2026-11-12): milestones, calibration separation, the official OG card."""

import csv
from datetime import UTC, datetime

from betdata.ledger import PICK_FIELDS, RESULT_FIELDS, milestones

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)


def _w(path, fields, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def _f_picks(out, n_threes, n_reb=0):
    picks, res = [], []
    for i in range(n_threes + n_reb):
        mk = "threes" if i < n_threes else "rebounds"
        picks.append({"pick_id": f"P{i}", "dedup_key": f"k{i}", "sport": "nba",
                      "game_id": str(i), "player": f"p{i}", "market": mk, "side": "over",
                      "line": "2.5", "odds": "-110", "signal": "snipe", "signal_name": "F",
                      "fair_prob": "0.6", "created_at": "2026-11-13T12:00:00+00:00"})
        res.append({"pick_id": f"P{i}", "result": "win" if i % 2 else "loss",
                    "profit": "0.9" if i % 2 else "-1", "graded_at": "2026-11-14T12:00:00+00:00"})
    _w(out / "picks.csv", PICK_FIELDS, picks)
    _w(out / "results.csv", RESULT_FIELDS, res)


def test_f_threes_milestones_count_threes_only(tmp_path):
    _f_picks(tmp_path, 59, n_reb=100)
    assert not [m for m in milestones(tmp_path, NOW) if m["signal_name"] == "F-threes"]
    _f_picks(tmp_path, 60, n_reb=100)
    got = [m for m in milestones(tmp_path, NOW) if m["signal_name"] == "F-threes"]
    assert [(m["signal_name"], m["graded"]) for m in got] == [("F-threes", "60")]
    assert not [m for m in milestones(tmp_path, NOW) if m["signal_name"] == "F-threes"]


def test_threes_never_on_the_official_og_card():
    """Rules v2 (pinned) already rejects any OG non-assist prop: threes flow to Action."""
    from betdata import eligibility as EL

    assert EL.reason("og", "threes", "over", 2.5, 0) == EL.NON_ASSIST
    assert EL.reason("og", "threes", "under", 1.5, None) == EL.NON_ASSIST
