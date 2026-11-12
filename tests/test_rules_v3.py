"""Rules v3 (owner 2026-11-12, MODEL.md "Rules v3"): rebound overs are eligible for the official
OG card from the Nov 13 card; rebound unders stay out ("filtered: rebound unders"); Next Gen, the
parlay and news keep Rules v2 exactly."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from betdata import action as AC
from betdata import eligibility as EL
from betdata import og_ranking as OR
from tests.test_action import NOW, TIP, _keys, _pick, _run, pdb  # noqa: F401


def _a(market, side, line=8.5, signal="snipe", tip=TIP):
    return SimpleNamespace(signal=signal, sport="nba", tip=tip, market=market, side=side,
                           line=line, player_name="P", game_id=1, details={},
                           created_at=tip - timedelta(hours=12))  # fmt: skip


@pytest.mark.parametrize("market,side,line,want", [
    ("rebounds", "over", 8.5, None),  # eligible again on OG
    ("rebounds", "under", 8.5, EL.REBOUND_UNDERS),
    ("assists", "over", 6.5, None),  # assists exactly as Rules v2
    ("assists", "under", 5.5, EL.UNDER_ASSISTS),
    ("assists", "under", 4.5, None),
    ("points", "over", 20.5, EL.NON_ASSIST),
    ("threes", "over", 2.5, EL.NON_ASSIST),
    ("spreads", "home", -3.5, EL.NOT_ASSISTS_OG),
])  # fmt: skip
def test_og_rules_v3(market, side, line, want, monkeypatch):
    monkeypatch.setattr(EL, "teammates_out", lambda *a: 0)
    assert EL.alert_reason(None, _a(market, side, line)) == want


def test_before_nov_13_rules_v2_unchanged(monkeypatch):
    monkeypatch.setattr(EL, "teammates_out", lambda *a: 0)
    nov12 = TIP - timedelta(days=1)
    for side in ("over", "under"):
        assert EL.alert_reason(None, _a("rebounds", side, tip=nov12)) == EL.REBOUNDS


@pytest.mark.parametrize("side", ["over", "under"])
def test_next_gen_keeps_rules_v2(side, monkeypatch):
    monkeypatch.setattr(EL, "teammates_out", lambda *a: 0)
    for sig in EL.NEXTGEN_SIGNALS:
        assert EL.alert_reason(None, _a("rebounds", side, signal=sig)) == EL.REBOUNDS
    assert EL.reason("nextgen", "rebounds", side, 8.5, 0) == EL.REBOUNDS  # parlay, news


def test_rebound_over_through_ranking_v2(pdb):  # noqa: F811
    with pdb.begin() as p:
        _pick(p, "reb_ok", player="R1", market="rebounds", line=8.5, median=9.5, fair=0.70)
        _pick(p, "reb_med", player="R2", market="rebounds", line=8.5, median=8.5, fair=0.72)
        _pick(p, "reb_price", player="R3", market="rebounds", line=8.5, median=10.0,
              price=-170, fair=0.71)  # fmt: skip
        _pick(p, "reb_under", player="R4", market="rebounds", side="under", line=8.5,
              median=7.5, fair=0.80)  # fmt: skip
        _pick(p, "ast", player="A1", fair=0.65)
    og, act = _run(pdb)
    assert _keys(og) == ["reb_ok", "ast"]  # ranked by probability, 0.5u
    assert all(x["stake"] == 0.5 for x in og["picks"])
    assert og["filtered"] == {"reb_under": EL.REBOUND_UNDERS}
    assert og["og_rejected"] == {"reb_med": OR.MEDIAN_SIDE, "reb_price": OR.PRICE}
    # Action: the rebound over on the card is excluded; the under and the overs that missed stay
    assert "reb_ok" not in _keys(act)
    assert set(_keys(act)) == {"reb_under", "reb_med", "reb_price"}
    reasons = {x["dedup_key"]: x["reasons"] for x in act["picks"]}
    assert reasons["reb_under"] == [AC.REBOUND_UNDERS]
    assert reasons["reb_med"] == [AC.MEDIAN] and reasons["reb_price"] == [AC.PRICE]


def test_top3_is_a_subset_and_rebound_over_counts(pdb):  # noqa: F811
    from betdata.alerts.db import get_state

    with pdb.begin() as p:
        for i in range(3):
            _pick(p, f"reb{i}", player=f"R{i}", market="rebounds", line=8.5, median=9.5,
                  fair=0.80 - i / 100)  # fmt: skip
        for i in range(3):
            _pick(p, f"ast{i}", player=f"A{i}", fair=0.70 - i / 100)
    og, _ = _run(pdb)
    with pdb.connect() as p:
        t3 = get_state(p, OR.top3_key("nba", NOW.date()))
        sh = get_state(p, OR.threes_key("nba", NOW.date()))
    assert _keys(og) == ["reb0", "reb1", "reb2", "ast0", "ast1"]
    assert _keys(t3) == ["reb0", "reb1", "reb2"]
    assert _keys(sh) == _keys(og)  # the OG + threes shadow follows Rules v3 too


def _row(day, side, filt=EL.REBOUNDS, signal="F", result="win"):
    return {"sport": "nba", "signal_name": signal, "day": day, "filter": filt, "side": side,
            "market": "rebounds", "result": result, "profit": "0.9091" if result == "win"
            else "-1.0", "odds": "-110", "clv_consensus": ""}  # fmt: skip


def test_filters_table_splits_rebounds_by_side(monkeypatch):
    from betdata import cards as C

    monkeypatch.setattr(C, "view", lambda rows, card: [])
    rows = [_row("2026-11-11", "over"), _row("2026-11-11", "under", result="loss"),
            _row("2026-11-12", "under"),
            _row("2026-11-13", "over"),  # stale Rules v2 annotation on a Rules v3 OG over
            _row("2026-11-13", "under", EL.REBOUND_UNDERS, result="loss"),
            _row("2026-11-13", "over", signal="nba-engine")]  # Next Gen: still filtered
    t = {(r["card"], r["reason"]): r for r in EL.filters_table(rows)}
    assert t[("OG", EL.REBOUND_OVERS)]["removed"] == 1  # Nov 11 only
    assert t[("OG", EL.REBOUND_UNDERS)]["removed"] == 3
    assert t[("OG", EL.REBOUND_UNDERS)]["wlp"] == "1-2-0"
    assert t[("Next Gen", EL.REBOUND_OVERS)]["removed"] == 1
    assert not any(r["reason"] == EL.REBOUNDS for r in t.values())


def test_rebound_overs_line(monkeypatch):
    import betdata.cards as C
    from betdata import og_review as RV

    rows = [{"day": "2026-11-13", "market": "rebounds", "side": "over", "result": "win",
             "profit": "0.9091", "card_stake": "0.5", "odds": "-110", "clv_consensus": ""},
            {"day": "2026-11-12", "market": "rebounds", "side": "over", "result": "loss",
             "profit": "-1.0", "card_stake": "0.5", "odds": "-110", "clv_consensus": ""}]
    monkeypatch.setattr(C, "view", lambda rows, card: rows)
    assert RV.rebound_overs_line(rows).startswith(
        "- Rebound overs (official, Rules v3 from Nov 13): 1-0-0, win 100.0%")
