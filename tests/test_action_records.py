"""The Action record (owner 2026-11-12): separate, graded only, 0.25u; breakdowns sum up."""


import pytest

from betdata import action as AC
from betdata import cards as C
from betdata import official as O
from tests.test_spread_records import _ledger, _pick, _w


def _with_action(out):
    _ledger(out)
    from betdata.ledger import PICK_FIELDS, RESULT_FIELDS, read_rows

    picks = read_rows(out / "picks.csv") + [
        _pick("a1", "snipe", "F", "rebounds", "over", "120", "8.5"),
        _pick("a2", "snipe", "F", "assists", "under", "-105", "6.5"),
        _pick("a3", "snipe", "F", "rebounds", "under", "110", "9.5"),
        _pick("a4", "snipe", "F", "assists", "over", "140", "3.5")]  # a4 pending
    res = read_rows(out / "results.csv") + [
        {"pick_id": "a1", "result": "win", "profit": "1.2"},
        {"pick_id": "a2", "result": "loss", "profit": "-1.0"},
        {"pick_id": "a3", "result": "win", "profit": "1.1"}]
    _w(out / "picks.csv", PICK_FIELDS, picks)
    _w(out / "results.csv", RESULT_FIELDS, res)
    rows = [{"pick_id": pid, "sport": "nba", "card_date": "2026-11-13", "action": "yes",
             "action_stake": "0.25", "market": mk, "reason": why, "reasons": why}
            for pid, mk, why in (("a1", "rebounds", "rebounds"),
                                 ("a2", "assists", "under 5.5+ assists"),
                                 ("a3", "rebounds", "rebounds"),
                                 ("a4", "assists", "outside price range"))]
    _w(out / "action_card.csv", AC.FIELDS, rows)


def test_action_record_separate_graded_only_quarter_unit(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _ledger(a)
    _with_action(b)
    ra, rb = O.dataset(a), O.dataset(b)
    v = C.view(rb, C.ACTION)
    assert sorted(r["pick_id"] for r in v) == ["a1", "a2", "a3", "a4"]
    rec = O.record(v, O.official_stake)  # a4 pending: never counted
    assert rec.wlp == "2-1-0" and rec.units == pytest.approx(0.25 * (1.2 - 1.0 + 1.1))
    for k in (C.NEXTGEN, C.OG, C.SPREAD):  # every other record unchanged
        assert [r["pick_id"] for r in C.view(ra, k)] == [r["pick_id"] for r in C.view(rb, k)]
        assert O.goal(C.view(ra, k)) == O.goal(C.view(rb, k))
    assert O.goal_rows(ra, "nba") == O.goal_rows(rb, "nba")
    assert not any(r["official"] for r in rb if r["pick_id"].startswith("a"))


def test_breakdowns_sum_to_the_total(tmp_path):
    _with_action(tmp_path)
    v = C.view(O.dataset(tmp_path), C.ACTION)
    total = O.record(v, O.official_stake)
    for by in ("reason", "market"):
        parts = AC.breakdown(v, by)
        assert sum(p.graded for p in parts.values()) == total.graded
        assert sum(p.units for p in parts.values()) == pytest.approx(total.units)
    assert set(AC.breakdown(v, "reason")) == {"rebounds", "under 5.5+ assists"}
    assert AC.breakdown(v, "market")["rebounds"].wlp == "2-0-0"
