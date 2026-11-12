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


def _guard_rows(reb_results):
    """Official OG rows from Nov 13: the given rebound-over results, 4 a day, each day ending
    with 5 assist wins (so the slump rules stay quiet)."""
    from tests.test_og_ranking import _og_rows

    days = []
    for i in range(0, len(reb_results), 4):
        days.append((f"2026-11-{13 + i // 4}", list(reb_results[i:i + 4]) + ["win"] * 5))
    rows = _og_rows(days)
    for r in rows:
        k = int(r["pick_id"][1:]) - 1
        r["market"], r["side"] = ("rebounds", "over") if k % 9 < 4 else ("assists", "over")
    return rows


def test_guardrail_fires_at_7_13_not_8_12():
    seven = _guard_rows(["win"] * 7 + ["loss"] * 13)
    assert OR.slump_rule(seven) is None
    assert OR.rebound_overs_guard(seven) == (7, 13)
    assert OR.guard_line(7, 13) == ("OG review: rebound overs 7-13 over last 20 — see "
                                    "reports/nba/og_ranking_v2.md")
    assert OR.rebound_overs_guard(_guard_rows(["win"] * 8 + ["loss"] * 12)) is None
    assert OR.rebound_overs_guard(_guard_rows(["loss"] * 16)) is None  # fewer than 20
    # only the last 20 decided count, pushes and voids skipped
    more = _guard_rows(["win"] * 4 + ["win"] * 7 + ["push", "void"] + ["loss"] * 13
                       + ["loss"] * 3)  # fmt: skip
    assert OR.rebound_overs_guard(more) == (4, 16)


def test_guardrail_once_a_day_with_the_slump_alert(tmp_path):
    from datetime import UTC, datetime

    from betdata.alerts.db import engine_for
    from tests.test_og_ranking import _days, _og_rows

    db = engine_for(tmp_path / "p.sqlite")
    sent = []

    class Client:
        def post(self, url, content, headers):
            sent.append((headers["Title"], content.decode()))
            return type("R", (), {"status_code": 200})()

    rows = _guard_rows(["win"] * 7 + ["loss"] * 13)
    now = datetime(2026, 11, 19, 11, 16, tzinfo=UTC)
    assert OR.slump_check(db, rows, now, "t", Client()) == "http 200"
    assert sent == [("OG review", "OG review: rebound overs 7-13 over last 20 — see "
                                  "reports/nba/og_ranking_v2.md")]
    slumping = _og_rows(_days(["loss"] * 9))  # the slump alert the same day: still once a day
    assert OR.slump_check(db, slumping, now, "t", Client()) is None
    assert OR.slump_check(db, rows, now, "t", Client()) is None
    assert len(sent) == 1
    # both fire on a later day: one alert, the slump line first
    both = rows + _og_rows([("2026-11-20", ["loss"] * 9)])
    assert OR.slump_check(db, both, now + timedelta(days=1), "t", Client()) == "http 200"
    assert len(sent) == 2 and "rebound overs" not in sent[-1][1]


def test_review_side_split(monkeypatch):
    import betdata.cards as C
    from betdata import og_review as RV

    def r(card, mk, sd, res):
        return {"card": card, "day": "2026-11-13", "market": mk, "side": sd, "result": res,
                "profit": "0.9" if res == "win" else "-1.0", "card_stake": "0.5",
                "odds": "-110", "clv_consensus": "", "action_stake": "0.25"}

    rows = [r("og", "rebounds", "over", "win"), r("og", "assists", "under", "loss"),
            r("action", "rebounds", "over", "loss"), r("action", "threes", "under", "win")]
    monkeypatch.setattr(C, "view", lambda rows, card: [x for x in rows if x["card"] == card])
    t = "\n".join(RV.side_split(rows))
    assert "Rebound overs (Action, from Nov 13): 0-1-0" in t
    assert "| rebounds · over | 1-0-0, +0.45u (100%) | 0-1-0" in t
    assert "| threes · under | 0-0-0, +0.00u | 1-0-0" in t
