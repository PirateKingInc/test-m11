"""The daily Next Gen parlay (owner decision 2026-11-06, betdata/parlay.py)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from betdata import morning as M
from betdata import parlay as PL
from betdata.alerts.db import engine_for, get_state, set_state

NOW = datetime(2026, 11, 6, 11, 15, tzinfo=UTC)  # 6:15 AM ET
TIP = datetime(2026, 11, 7, 0, 30, tzinfo=UTC)


def leg(gid, p, price, book="draftkings", pm=None, market="points", player=None):
    return PL.Leg(gid, f"A{gid} @ H{gid}", TIP.isoformat(), market, "over", 20.5,
                  player or f"P{gid}", price, book, p, pm if pm is not None else p - 0.05)


def cand(p, price, book="draftkings", pm=None, raw=None):
    """A candidate: blended `p` (fair), market `pm`, the engine's raw probability `raw`."""
    return SimpleNamespace(book=book, price=price, fair=p, p_engine=p if raw is None else raw,
                           p_market=pm if pm is not None else p - 0.05)  # fmt: skip


# --- leg rules ----------------------------------------------------------------------------------


def test_leg_rules():
    assert PL.qualifies(cand(0.72, -150), 0.70)
    assert not PL.qualifies(cand(0.72, -210), 0.70)  # price below -200
    assert not PL.qualifies(cand(0.72, 105), 0.70)  # price above +100
    assert PL.qualifies(cand(0.72, 100), 0.70) and PL.qualifies(cand(0.72, -200), 0.70)
    assert not PL.qualifies(cand(0.72, -150, pm=0.75), 0.70)  # below the market's no-vig
    assert not PL.qualifies(cand(0.69, -150), 0.70)  # below the bar
    assert not PL.qualifies(cand(0.72, -150, book="pinnacle"), 0.70)  # DK / FD only


# --- selection -------------------------------------------------------------------------------


def test_three_leg_default_one_leg_per_game():
    legs = [leg(1, 0.74, -150), leg(1, 0.78, -160, player="Q1"),  # one leg per game: the best
            leg(2, 0.72, -140), leg(3, 0.71, -120), leg(4, 0.66, 100)]  # fmt: skip
    p = PL.choose(legs)
    assert p.kind == 3 and sorted(x.game_id for x in p.legs) == [1, 2, 3]
    assert {x.player for x in p.legs if x.game_id == 1} == {"Q1"}
    assert p.ev == pytest.approx(p.p_hit * p.decimal - 1)
    assert p.p_hit == pytest.approx(0.78 * 0.72 * 0.71)


def test_two_leg_only_if_ev_at_least_3_points_higher():
    # three 70%+ legs at short prices, and two 65%+ legs at long prices
    base = [leg(1, 0.71, -200), leg(2, 0.71, -200), leg(3, 0.71, -200)]
    three = PL.best(base, 3)
    two_legs = [leg(4, 0.66, 100), leg(5, 0.66, 100)]
    two = PL.best(base + two_legs, 2)
    assert two.ev >= three.ev + 0.03
    assert PL.choose(base + two_legs).kind == 2
    # a 2-leg less than 3 points better: the 3-leg stays
    close = [leg(4, 0.70, -175), leg(5, 0.70, -175)]
    t3, t2 = PL.best(base + close, 3), PL.best(base + close, 2)
    assert t2.ev < t3.ev + 0.03 and PL.choose(base + close).kind == 3


def test_two_leg_when_no_three_leg_and_none_when_nothing():
    assert PL.choose([leg(1, 0.67, -110), leg(2, 0.66, -105)]).kind == 2
    assert PL.choose([leg(1, 0.67, -110), leg(1, 0.68, -110)]) is None  # one game only
    assert PL.choose([]) is None


def test_priced_entirely_at_one_book_the_better_ev():
    legs = [leg(1, 0.75, -150, "draftkings"), leg(2, 0.75, -150, "draftkings"),
            leg(3, 0.75, -150, "draftkings"),
            leg(1, 0.75, -130, "fanduel"), leg(2, 0.75, -130, "fanduel"),
            leg(3, 0.75, -200, "fanduel")]  # fmt: skip
    p = PL.choose(legs)
    assert p.book in ("draftkings", "fanduel") and {x.book for x in p.legs} == {p.book}
    dk, fd = (PL.best_at_book([x for x in legs if x.book == b], 3) for b in PL.BOOKS)
    assert p.ev == pytest.approx(max(dk.ev, fd.ev))
    assert p.book == ("fanduel" if fd.ev > dk.ev else "draftkings")
    # a leg missing at FanDuel: FanDuel cannot price the 3-leg
    assert PL.best_at_book([x for x in legs if x.book == "fanduel" and x.game_id != 3], 3) \
        is None


# --- grading -----------------------------------------------------------------------------------


def test_settle_wins_losses_pushes_voids():
    d = [PL.decimal(-150), PL.decimal(-120), PL.decimal(100)]
    r, prof, left, paid = PL.settle(["win", "win", "win"], d)
    assert r == "win" and left == 3 and prof == pytest.approx(d[0] * d[1] * d[2] - 1)
    assert PL.settle(["win", "loss", "win"], d)[:2] == ("loss", -1.0)
    # a push drops out: re-priced at the two remaining legs
    r, prof, left, _ = PL.settle(["win", "push", "win"], d)
    assert (r, left) == ("win", 2) and prof == pytest.approx(d[0] * d[2] - 1)
    # fewer than 2 legs left: the single remaining leg
    r, prof, left, _ = PL.settle(["void", "push", "win"], d)
    assert (r, left) == ("win", 1) and prof == pytest.approx(d[2] - 1)
    assert PL.settle(["void", "push", "push"], d)[:3] == ("void", 0.0, 0)
    assert PL.settle(["void", "loss", "push"], d)[0] == "loss"  # the single leg lost


def test_grade_and_record(monkeypatch, tmp_path):
    doc = {"day": "2026-11-06", "chosen_at": NOW.isoformat(), "stake": 0.25, "kind": 3,
           "legs": [{"price": -150}, {"price": -120}, {"price": 100}]}
    results = iter(["win", "push", "win"])
    monkeypatch.setattr(PL, "leg_result", lambda m, x: next(results))
    g = PL.grade(None, doc)
    assert g["result"] == "win" and g["legs_left"] == 2
    assert g["units"] == pytest.approx(0.25 * (PL.decimal(-150) * PL.decimal(100) - 1), abs=1e-4)
    from betdata.ledger import append_rows

    append_rows(tmp_path / PL.RESULTS, PL.RESULT_FIELDS, [
        {"parlay_id": "a", "day": "d1", "result": "win", "units": "0.5"},
        {"parlay_id": "b", "day": "d2", "result": "loss", "units": "-0.25"},
        {"parlay_id": "c", "day": "d3", "result": "void", "units": "0"}], key="parlay_id")
    rec = PL.record(tmp_path)
    assert (rec.wins, rec.losses, rec.voids, rec.graded) == (1, 1, 1, 2)
    assert rec.text() == "Parlay record: 1-1 · +0.25u · ROI +50.0% · 2 graded"
    monkeypatch.setattr(PL, "leg_result", lambda m, x: None)
    assert PL.grade(None, doc) is None  # a leg still pending


# --- selection once, the ledger, the push --------------------------------------------------------


def test_chosen_once_written_once(tmp_path, monkeypatch):
    pdb = engine_for(tmp_path / "p.sqlite")
    picks = [leg(1, 0.74, -150), leg(2, 0.72, -140), leg(3, 0.71, -120)]
    monkeypatch.setattr(PL, "candidate_legs", lambda poller, main, now: picks)
    doc = PL.select(pdb, None, None, NOW)
    assert doc["kind"] == 3 and len(doc["legs"]) == 3 and doc["stake"] == 0.25
    monkeypatch.setattr(PL, "candidate_legs", lambda *a: [])
    assert PL.select(pdb, None, None, NOW + timedelta(hours=1)) == doc  # once a day
    out = tmp_path / "ledger"
    out.mkdir()
    from betdata.ledger import read_rows

    with pdb.connect() as p:
        PL.sync(p, None, out, NOW)
        PL.sync(p, None, out, NOW)
    [row] = read_rows(out / PL.FILE)
    assert row["parlay_id"] == "ng-parlay:2026-11-06" and row["signal"] == "ng-parlay"
    lines = PL.push_lines(doc)
    assert lines[0].startswith("Parlay (3 legs, DraftKings): +") and "0.25u" in lines[0]
    assert "hits about 1 in" in lines[0] and len(lines) == 4
    assert "Engine 74% · Market 69%" in lines[1]


def test_no_parlay_today(tmp_path, monkeypatch):
    pdb = engine_for(tmp_path / "p.sqlite")
    monkeypatch.setattr(PL, "candidate_legs", lambda *a: [leg(1, 0.66, -110)])
    doc = PL.select(pdb, None, None, NOW)
    assert doc["none"] and PL.push_lines(doc) == ["Parlay: No parlay today: no legs met the bar"]
    with pdb.connect() as p:
        view = PL.page_doc(p, None, NOW.date(), tmp_path)
    assert view["none"] and view["note"] == "No parlay today: no legs met the bar"


def test_hits_text():
    assert PL.hits_text(0.36) == "hits about 1 in 3" and PL.hits_text(0.24) == "hits about 1 in 4"


# --- fail-safe ---------------------------------------------------------------------------------


def test_a_parlay_error_never_stops_the_cards_page_or_push(tmp_path, monkeypatch):
    pdb = engine_for(tmp_path / "p.sqlite")
    calls = []
    monkeypatch.setattr(M, "sports_due", lambda main, now: ["nba"])
    monkeypatch.setattr(M, "grade", lambda *a: None)
    monkeypatch.setattr(M, "ledger_sync", lambda *a: None)
    monkeypatch.setattr(M, "engine", lambda *a: 0)
    monkeypatch.setattr(M, "select_cards", lambda *a: calls.append("select") or ([], []))

    def boom(*a):
        raise RuntimeError("no lines")

    monkeypatch.setattr(M, "parlay_select", boom)
    rep = M.run(None, pdb, None, NOW, out=tmp_path, build=lambda: calls.append("page"))
    assert rep.steps == list(M.STEPS) and calls == ["select", "page"]
    assert rep.parlay is None and rep.errors == ["parlay: RuntimeError: no lines"]
    with pdb.connect() as p:
        _t, body = M.message(p, [], "nba", NOW)  # the push goes out without a parlay section
    assert "Parlay" not in body and body.startswith("NBA · Fri Nov 6")


def test_push_section_after_next_gen(tmp_path):
    pdb = engine_for(tmp_path / "p.sqlite")
    doc = {"day": "2026-11-06", "chosen_at": NOW.isoformat(), "stake": 0.25, "kind": 2,
           "book": "fanduel", "decimal": 3.2, "american": 220, "ev": 0.4, "p_hit": 0.44,
           "legs": [PL.Leg(1, "ORL @ MIA", TIP.isoformat(), "points", "over", 20.5,
                           "Bam Adebayo", -110, "fanduel", 0.68, 0.6).__dict__,
                    PL.Leg(2, "BOS @ NYK", TIP.isoformat(), "totals", "under", 221.5, None,
                           -105, "fanduel", 0.67, 0.55).__dict__]}  # fmt: skip
    with pdb.begin() as p:
        set_state(p, PL.key(NOW.date()), doc, NOW)
    with pdb.connect() as p:
        _t, body = M.message(p, [], "nba", NOW)
        assert get_state(p, PL.key(NOW.date()))["kind"] == 2
    lines = body.split("\n")
    i = next(k for k, x in enumerate(lines) if x.startswith("Next Gen:"))
    j = next(k for k, x in enumerate(lines) if x.startswith("Parlay"))
    assert j > i and lines[j] == ("Parlay (2 legs, FanDuel): +220, 0.25u · hits about 1 in 2")
    assert lines[j + 1].startswith("  ORL @ MIA 7:30 PM ET: Bam Adebayo over 20.5 points -110")
    assert lines[j + 2].startswith("  BOS @ NYK 7:30 PM ET: Under 221.5")


# --- agreement = the engine's raw probability (owner 2026-11-09) ----------------------------------


def test_agreement_on_the_raw_engine_value_floor_and_ev_on_the_blend():
    # raw 72% clears the 3-leg agreement bar though the blend is only 58%: a leg, if the blend
    # is at or above the market's no-vig (the value floor)
    assert PL.qualifies(cand(0.58, -120, pm=0.55, raw=0.72), 0.70)
    assert not PL.qualifies(cand(0.58, -120, pm=0.60, raw=0.72), 0.70)  # below the market
    assert not PL.qualifies(cand(0.80, -120, pm=0.55, raw=0.69), 0.70)  # blend high, raw low

    def lg(gid, blend, raw_, price=-120):
        x = leg(gid, blend, price)
        x.p_raw = raw_
        return x

    legs = [lg(1, 0.58, 0.72), lg(2, 0.57, 0.71), lg(3, 0.56, 0.74), lg(4, 0.90, 0.60)]
    p = PL.choose(legs)
    assert p.kind == 3 and sorted(x.game_id for x in p.legs) == [1, 2, 3]  # not game 4
    # EV from the blended probabilities (conservative), not the raw ones
    assert p.p_hit == pytest.approx(0.58 * 0.57 * 0.56)
    assert p.ev == pytest.approx(0.58 * 0.57 * 0.56 * PL.decimal(-120) ** 3 - 1)


def test_both_probabilities_stored_and_shown(tmp_path, monkeypatch):
    pdb = engine_for(tmp_path / "p.sqlite")
    legs = []
    for gid, raw_ in ((1, 0.74), (2, 0.72), (3, 0.71)):
        x = leg(gid, 0.6, -110)
        x.p_raw = raw_
        legs.append(x)
    monkeypatch.setattr(PL, "candidate_legs", lambda *a: legs)
    doc = PL.select(pdb, None, None, NOW)
    assert [(x["p"], x["p_raw"]) for x in doc["legs"]] == [(0.6, 0.74), (0.6, 0.72), (0.6, 0.71)]
    out = tmp_path / "ledger"
    out.mkdir()
    import json

    from betdata.ledger import read_rows

    with pdb.connect() as p:
        PL.sync(p, None, out, NOW)
    [row] = read_rows(out / PL.FILE)
    assert [(x["p"], x["p_raw"]) for x in json.loads(row["legs"])][0] == (0.6, 0.74)
    assert "Engine 74% · Market 55%" in PL.push_lines(doc)[1]
