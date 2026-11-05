"""M20 Part A: the three checks (betdata/checks.py) and the G1/G2 retirement."""

import contextlib
import csv
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from betdata import checks as K
from betdata.alerts import poller as P
from betdata.official import dataset
from betdata.page.official import research_html

LEDGER = Path(__file__).resolve().parents[1] / "ledger"


def _row(p, close, win, odds, cons=None, book=None, name="X"):
    book = close - 0.01 if book is None else book
    return {"signal_name": name, "result": "win" if win else "loss", "fair_prob": str(p),
            "book_prob": str(book), "clv_consensus": str(close - book), "odds": str(odds),
            "consensus_at_pick": "" if cons is None else json.dumps({"prob": cons})}


def test_ev_at_close_and_brier_by_hand():
    rows = [_row(0.6, 0.5, 1, 110), _row(0.6, 0.5, 0, 110)]
    c = K.three_checks(K.picks_of(rows))
    assert math.isclose(c.ev.value, 0.5 * 2.1 - 1)  # 5% EV at close on both
    # Brier: model (0.16 + 0.36)/2 = 0.26; close 0.25 -> +0.01
    assert math.isclose(c.brier.value, 0.01)
    assert c.calibration.note == "gap +10.0 pp"


def test_works_needs_all_three_and_200():
    good = [_row(0.55, 0.56, i % 100 < 56, 100) for i in range(400)]  # calibrated, EV +12%
    c = K.three_checks(K.picks_of(good))
    assert c.ev.passed and c.calibration.passed
    assert c.brier.passed is False  # the model is not closer than the close here
    assert not c.works and c.status.startswith("does not work")
    few = K.three_checks(K.picks_of(good[:50]))
    assert few.status.startswith("too few (50/200)")


def test_clv_split_adds_up():
    rows = [_row(0.55, 0.52, 1, -110, cons=0.50, book=0.48)]
    c = K.three_checks(K.picks_of(rows))
    assert math.isclose(c.move[0] + c.book_diff[0], c.clv[0])
    assert math.isclose(c.move[0], 0.02) and math.isclose(c.book_diff[0], 0.02)


def test_pushes_and_voids_are_left_out():
    rows = [_row(0.5, 0.5, 1, 100), {**_row(0.5, 0.5, 1, 100), "result": "push"},
            {**_row(0.5, 0.5, 1, 100), "result": "void"}]
    assert K.three_checks(K.picks_of(rows)).n == 1


def test_real_ledger_matches_an_independent_recount():
    """F's EV at close from the raw CSVs in plain code equals the module's."""
    res = {r["pick_id"]: r for r in csv.DictReader((LEDGER / "results.csv").open())}
    evs = []
    for p in csv.DictReader((LEDGER / "picks.csv").open()):
        r = res.get(p["pick_id"])
        if p["signal_name"] != "F" or not r or r["result"] not in ("win", "loss"):
            continue
        if r["clv_consensus"] == "":
            continue
        close = float(p["book_prob"]) + float(r["clv_consensus"])
        o = float(p["odds"])
        evs.append(close * (1 + (o / 100 if o > 0 else 100 / -o)) - 1)
    c = K.by_signal(dataset(LEDGER))["F"]
    assert c.ev.n == len(evs) and math.isclose(c.ev.value, sum(evs) / len(evs))


def test_research_tab_shows_three_checks_and_retired(tmp_path):
    html = research_html(dataset(LEDGER), tmp_path)
    assert "Three checks: Calibration" in html
    assert "data-signal='G1'><div><b>G1</b> <span class='meta'>retired" in html
    # F is archived (owner override 2026-11-04), not retired: its history stays
    assert ("data-signal='F'><div><b>F</b> <span class='meta archived'>archived 2026-11-04 "
            "(nbap15): no new picks, history kept</span>") in html


def test_poller_cycle_no_longer_runs_g1(monkeypatch):
    called = []
    me = SimpleNamespace(main=SimpleNamespace(connect=lambda: contextlib.nullcontext()),
                         nfl_handler=None, _due=lambda *a: False,
                         _guard=lambda what, fn, *a: called.append(what))  # fmt: skip
    for name in ("lag_polls", "sharp_boards", "closing_polls", "sweep", "g1_check", "card",
                 "tracker", "nba_report"):
        setattr(me, name, None)
    monkeypatch.setattr("betdata.alerts.nba.active", lambda c, t: False)
    P.Poller.cycle(me, datetime(2026, 11, 4, 0, 0, tzinfo=UTC))
    assert "g1" not in called and "sweep" in called
    assert P.RETIRED == {"g1", "g2", "snipe"}  # F archived 2026-11-04
