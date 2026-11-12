"""F's calibration (owner 2026-11-12, betdata/f_calibration.py; MODEL.md "F calibration")."""

import csv
import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from betdata import f_calibration as FC
from betdata import og_ranking as OR
from betdata.calibration import Fit
from betdata.ledger import ANNOTATION_FIELDS, PICK_FIELDS, RESULT_FIELDS, read_rows

THU = datetime(2026, 11, 12, 15, tzinfo=UTC)
MON = datetime(2026, 11, 16, 15, tzinfo=UTC)


def _w(path, fields, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def _ledger(out, n, p_win=lambda p: p - 0.05, seed=1, made="2026-11-05T12:00:00+00:00",
            extra=()):
    """n F picks (one per player and game) with results drawn from p_win(model p)."""
    rng = random.Random(seed)
    picks, res = [], []
    for i in range(n):
        p = rng.uniform(0.42, 0.65)
        picks.append({"pick_id": f"P{i}", "dedup_key": f"k{i}", "created_at": made,
                      "sport": "nba", "game_id": str(i), "player": f"pl{i}", "market": "assists",
                      "side": "over", "line": "4.5", "odds": "-110", "signal": "snipe",
                      "signal_name": "F", "fair_prob": f"{p:.4f}"})
        res.append({"pick_id": f"P{i}", "result": "win" if rng.random() < p_win(p) else "loss",
                    "profit": "0", "graded_at": "2026-11-06T12:00:00+00:00"})
    picks += list(extra)
    _w(out / "picks.csv", PICK_FIELDS, picks)
    _w(out / "results.csv", RESULT_FIELDS, res)
    if not (out / "annotations.csv").exists():
        _w(out / "annotations.csv", ANNOTATION_FIELDS, [])


def test_first_fit_any_day_then_mondays_only(tmp_path):
    _ledger(tmp_path, 400)
    f = FC.maybe_refit(tmp_path, THU)
    assert f is not None and f.slope > 0 and f.n == 400 and f.fit_date == "2026-11-12"
    assert FC.maybe_refit(tmp_path, THU + timedelta(days=1)) is None  # Friday: no refit
    assert FC.maybe_refit(tmp_path, MON) is not None  # Monday: refit
    assert FC.maybe_refit(tmp_path, MON + timedelta(hours=2)) is None  # once that Monday
    rows = read_rows(tmp_path / FC.FILE)
    assert [(r["model"], r["fit_date"], r["status"]) for r in rows] == [
        ("F", "2026-11-12", "ok"), ("F", "2026-11-16", "ok")]
    assert set(rows[0]) == set(FC.FIELDS)


def test_too_few_or_not_increasing_keeps_the_previous_fit(tmp_path, monkeypatch):
    _ledger(tmp_path, 150)
    assert FC.maybe_refit(tmp_path, THU) is None  # n < 200: rejected
    (r,) = read_rows(tmp_path / FC.FILE)
    assert (r["status"], r["reason"]) == ("rejected", "n 150 < 200")
    assert FC.current(tmp_path) is None
    _ledger(tmp_path, 400)
    good = FC.maybe_refit(tmp_path, MON)
    assert good is not None
    # a decreasing fit next Monday: rejected, the previous fit stays in force
    monkeypatch.setattr(FC, "fit", lambda ps, ys: (-0.3, 0.1))
    assert FC.maybe_refit(tmp_path, MON + timedelta(days=7)) is None
    last = read_rows(tmp_path / FC.FILE)[-1]
    assert last["status"] == "rejected" and last["reason"].startswith("slope -0.3000 <= 0")
    assert FC.current(tmp_path) == good
    monkeypatch.setattr(FC, "fit", lambda ps, ys: (0.0, 0.1))  # slope 0: rejected too
    assert FC.maybe_refit(tmp_path, MON + timedelta(days=14)) is None
    assert FC.current(tmp_path) == good


def test_fit_is_on_one_pick_per_player_per_game(tmp_path):
    dup = {"pick_id": "Pdup", "dedup_key": "kdup", "created_at": "2026-11-05T12:00:00+00:00",
           "sport": "nba", "game_id": "0", "player": "pl0", "market": "assists",
           "side": "over", "line": "3.5", "odds": "-110", "signal": "snipe",
           "signal_name": "F", "fair_prob": "0.30"}  # same player and game, lower p
    _ledger(tmp_path, 210, extra=[dup])
    with (tmp_path / "results.csv").open("a") as f:
        f.write("Pdup,win,0,,,,2026-11-06T12:00:00+00:00\n")
    ps, ys = FC.points(tmp_path)
    assert len(ps) == 210 and 0.30 not in ps


def test_f_cal_annotations_new_picks_only_write_once(tmp_path):
    old = {"pick_id": "Pold", "dedup_key": "old", "created_at": "2026-11-12T10:00:00+00:00",
           "signal_name": "F", "fair_prob": "0.52"}
    new = {"pick_id": "Pnew", "dedup_key": "new", "created_at": "2026-11-12T16:00:00+00:00",
           "signal_name": "F", "fair_prob": "0.52"}
    cnba = {"pick_id": "Pc", "dedup_key": "c", "created_at": "2026-11-12T16:00:00+00:00",
            "signal_name": "C-NBA", "fair_prob": "0.52"}
    _ledger(tmp_path, 300, extra=[old, new, cnba])
    f = FC.maybe_refit(tmp_path, THU)  # fitted at 15:00 UTC
    assert FC.annotate(tmp_path, THU) == 1
    assert FC.annotate(tmp_path, THU + timedelta(hours=1)) == 0  # write-once
    (n,) = [r for r in read_rows(tmp_path / "annotations.csv") if r["field"] == "f_cal"]
    assert n["pick_id"] == "Pnew" and n["value"] == f"{f(0.52):.4f}"
    assert "2026-11-12" in n["note"]
    # a later fit never rewrites it
    FC.maybe_refit(tmp_path, MON)
    FC.annotate(tmp_path, MON)
    rows = [r for r in read_rows(tmp_path / "annotations.csv") if r["pick_id"] == "Pnew"]
    assert len(rows) == 1 and rows[0]["value"] == n["value"]


def test_the_monday_job_runs_it(tmp_path):
    """ledger.sync runs the F refit right after the parlay's refit, in the same job."""
    import inspect

    from betdata import ledger

    src = inspect.getsource(ledger.sync)
    i, j = src.index("maybe_refit(out, now)"), src.index("FC.maybe_refit(out, now)")
    assert i < j and "FC.annotate(out, now)" in src


# --- order preservation -------------------------------------------------------------------------


def _cand(i, p, price, minute, game=None, player=None):
    return SimpleNamespace(fair_prob=p, price=price, game_id=game or i,
                           player_name=player or f"p{i}", dedup_key=f"k{i}",
                           created_at=datetime(2026, 11, 12, 10, minute, tzinfo=UTC))


@pytest.mark.parametrize("seed", range(300))
def test_property_ranking_identical_raw_vs_any_increasing_fit(seed):
    rng = random.Random(seed)
    n = rng.randint(1, 25)
    ps = [round(rng.uniform(0.3, 0.8), rng.choice([2, 3, 4])) for _ in range(n)]  # ties happen
    cands = [_cand(i, ps[i], rng.choice([-150, -120, -110, 100, 115, 130]), rng.randint(0, 59),
                   game=rng.randint(1, 4), player=f"p{rng.randint(1, 8)}") for i in range(n)]
    fit = Fit("x", 300, rng.uniform(0.05, 3.0), rng.uniform(-2.0, 2.0))
    raw, rej_raw = OR.order(cands, None)
    cal, rej_cal = OR.order(cands, fit)
    assert [a.dedup_key for a in raw] == [a.dedup_key for a in cal]
    assert rej_raw == rej_cal


def test_strictly_increasing_on_the_probability_grid():
    fit = Fit("x", 544, 1.217649, -0.237674)
    grid = [i / 10000 for i in range(1, 10000)]
    vals = [fit(p) for p in grid]
    assert all(b > a for a, b in zip(vals, vals[1:], strict=False))
    assert fit(0.52) == pytest.approx(0.465, abs=0.001)  # the first live fit: 52% -> 46.5%
