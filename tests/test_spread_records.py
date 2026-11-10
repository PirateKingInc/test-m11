"""Spread Picks records (owner decision 2026-11-10): graded at the real price, 1 unit flat,
and fully separate from Next Gen, OG, their goals and the research tables."""

import csv

import pytest

from betdata import cards as C
from betdata import official as O
from betdata.alerts import grade as G
from betdata.ledger import (
    CARD_FIELDS,
    PICK_FIELDS,
    RESULT_FIELDS,
    SHADOW_FIELDS,
    breakdowns,
    counted,
    joined,
    milestones,
)
from betdata.spread import SPREAD_FIELDS


def _w(path, fields, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def _pick(pid, signal, name, market, side, odds, line="", day="2026-11-09"):
    return {"pick_id": pid, "dedup_key": pid, "created_at": f"{day}T04:30:00+00:00",
            "sport": "nba", "game_id": "7", "game": "CLE @ CHA", "tip": f"{day}T23:00:00+00:00",
            "market": market, "player": "", "side": side, "line": line, "odds": odds,
            "book": "draftkings", "signal": signal, "signal_name": name, "reason": "r"}


def _ledger(out, with_spread=True):
    picks = [_pick("ml", "engine", "nba-engine", "h2h", "home", "284"),
             _pick("ng2", "engine", "nba-engine", "totals", "over", "-110", "220.5"),
             _pick("og1", "snipe", "F", "assists", "over", "120", "6.5")]
    results = [{"pick_id": "ml", "result": "win", "profit": "2.84"},
               {"pick_id": "ng2", "result": "loss", "profit": "-1.0"},
               {"pick_id": "og1", "result": "loss", "profit": "-1.0"}]
    if with_spread:
        picks.append(_pick("sp", "dogsprd", "dog-spread", "spreads", "home", "-108", "8.5"))
        # CHA +8.5 at -108, final CHA 100 CLE 105: covers by 3.5 -> win at the real price
        res = G.result_of("spreads", "home", 8.5, 100, 105)
        from betdata import moneyline as ml

        results.append({"pick_id": "sp", "result": res, "profit": str(ml.profit(res, -108))})
    _w(out / "picks.csv", PICK_FIELDS, picks)
    _w(out / "results.csv", RESULT_FIELDS, results)
    _w(out / "card.csv", CARD_FIELDS, [
        {"pick_id": "ml", "sport": "nba", "card_date": "2026-11-09", "card": "yes",
         "card_label": "LEAN", "card_stake": "0.5"},
        {"pick_id": "ng2", "sport": "nba", "card_date": "2026-11-09", "card": "yes",
         "card_label": "TAKE", "card_stake": "1.0"}])  # fmt: skip
    _w(out / "shadow_card.csv", SHADOW_FIELDS, [
        {"pick_id": "og1", "sport": "nba", "card_date": "2026-11-09", "shadow": "yes",
         "shadow_label": "LEAN", "shadow_stake": "0.5"}])  # fmt: skip
    if with_spread:
        _w(out / "spread_card.csv", SPREAD_FIELDS, [
            {"pick_id": "sp", "sport": "nba", "card_date": "2026-11-09", "spread": "yes",
             "spread_label": "SPREAD", "spread_stake": "1.0", "trigger_pick_id": "ml",
             "book": "draftkings", "backfill": "true"}])  # fmt: skip


def test_profit_at_the_real_price():
    from betdata import moneyline as ml

    assert G.result_of("spreads", "home", 8.5, 100, 105) == "win"
    assert ml.profit("win", -108) == pytest.approx(100 / 108)  # never an assumed -110
    assert ml.profit("win", +105) == pytest.approx(1.05)
    assert ml.profit("loss", -108) == -1 and ml.profit("push", -108) == 0


def test_spread_record_is_its_own(tmp_path):
    _ledger(tmp_path)
    rows = O.dataset(tmp_path)
    sp = C.view(rows, C.SPREAD)
    assert [r["pick_id"] for r in sp] == ["sp"]
    assert sp[0]["day"] == "2026-11-09" and sp[0]["spread_backfill"] is True
    rec = O.record(sp, O.official_stake)
    assert rec.wlp == "1-0-0" and rec.units == pytest.approx(100 / 108)


def test_record_isolation(tmp_path):
    """Next Gen, OG, both goals, the research tables and the milestones are the same with or
    without Spread picks in the ledger."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _ledger(a, with_spread=False)
    _ledger(b, with_spread=True)
    ra, rb = O.dataset(a), O.dataset(b)
    for k in (C.NEXTGEN, C.OG):
        va, vb = C.view(ra, k), C.view(rb, k)
        assert [r["pick_id"] for r in va] == [r["pick_id"] for r in vb]
        assert O.goal(va) == O.goal(vb)
        assert O.record(va, O.official_stake).units == O.record(vb, O.official_stake).units
    assert O.goal_rows(ra, "nba") == [r for r in O.goal_rows(rb, "nba")
                                      if r["pick_id"] != "sp"]  # fmt: skip
    assert "sp" not in {r["pick_id"] for r in O.goal_rows(rb, "nba", C.OG)}
    assert set(O.signals(ra)) == set(O.signals(rb))  # no research row
    assert "dog-spread" not in set(O.signals(rb))
    ba, bb = breakdowns(counted(joined(a))), breakdowns(counted(joined(b)))
    assert ba["by signal"].keys() == bb["by signal"].keys()
    assert ba["all"]["all picks"].picks == bb["all"]["all picks"].picks
    assert not [m for m in milestones(b, None) if m["signal_name"] == "dog-spread"]
    assert not any(r["official"] for r in rb if r["pick_id"] == "sp")  # not a card.csv pick


def test_parlay_never_takes_a_spread_pick():
    """The parlay's legs come from Next Gen's engine candidates only (betdata/parlay.py)."""
    import inspect

    from betdata import parlay

    src = inspect.getsource(parlay.candidate_legs)
    assert "dogsprd" not in src and "spread_card" not in src
