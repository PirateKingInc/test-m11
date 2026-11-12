"""Rules v3 evidence report (report only; betdata/rules_v3_evidence.py)."""

from betdata import rules_v3_evidence as RE


def _r(pid, mk, sd, line, med, res, created="2026-11-01T10:00", game="1", player="A",
       day="2026-11-01"):
    return {"pick_id": pid, "signal_name": "F", "day": day, "market": mk, "side": sd,
            "line": str(line), "proj_median": str(med), "result": res, "game_id": game,
            "player": player, "created_at": created, "odds": "-110",
            "profit": "0.9091" if res == "win" else "-1.0", "clv_consensus": ""}


def test_live_split_one_per_player_game_side_and_median_rule():
    rows = [_r("a", "rebounds", "over", 8.5, 9.0, "win"),
            _r("a2", "rebounds", "over", 9.5, 9.0, "loss", created="2026-11-01T11:00"),  # dup
            _r("b", "rebounds", "over", 8.5, 8.0, "loss", player="B"),  # median removes
            _r("c", "rebounds", "under", 8.5, 8.0, "loss", player="C"),
            _r("d", "assists", "under", 4.5, 5.0, "win", player="D"),  # median removes
            _r("old", "rebounds", "over", 8.5, 9.0, "win", player="E", day="2026-10-28")]
    s = RE.live_split(rows)
    assert (s["rebounds"]["over"]["kept"]["w"], s["rebounds"]["over"]["kept"]["l"]) == (1, 0)
    assert s["rebounds"]["over"]["removed"]["l"] == 1
    assert s["rebounds"]["over"]["all"]["graded"] == 2  # the duplicate and Oct 28 left out
    assert s["rebounds"]["under"]["kept"]["l"] == 1
    assert s["assists"]["under"]["removed"]["w"] == 1


def _rec(w, l, units, roi=None):  # noqa: E741
    return {"w": w, "l": l, "p": 0, "win": w / (w + l), "units": units, "roi": roi,
            "per_day": 1.0}


def test_agreement_says_plainly():
    live = {"rebounds": {"over": {"kept": {"w": 27, "l": 16, "win": 0.628, "units": 16.5}},
                         "under": {"all": {"w": 127, "l": 173, "win": 0.423, "units": -28.0}}}}
    agree = {"pretip": {"ranking_v2_by_side": {"over": {"all": _rec(60, 40, 12.0)},
                                               "under": {"all": _rec(45, 55, -14.0)}},
                        "card_top5": {"assists_alone": _rec(5, 5, 0.0, 0.01),
                                      "assists_plus_rebound_overs": _rec(6, 4, 1.0, 0.05)}}}
    t = RE.agreement(live, agree)
    assert "**agrees** on the side split" in t and "at least as well" in t
    no = {"pretip": {**agree["pretip"], "ranking_v2_by_side": {
        "over": {"all": _rec(40, 60, -10.0)}, "under": {"all": _rec(55, 45, 8.0)}}}}
    assert "**does not agree**" in RE.agreement(live, no)
    md = RE.report({"rebounds": {sd: {k: _rec(1, 1, 0.0) for k in ("kept", "removed", "all")}
                                 for sd in ("over", "under")},
                    "assists": {sd: {k: _rec(1, 1, 0.0) for k in ("kept", "removed", "all")}
                                for sd in ("over", "under")}}, {}, "2026-11-12")
    assert "Report only, hindsight evidence" in md and "## Do the old seasons agree?" in md


def test_agreement_plain_verdict():
    live = {"rebounds": {"over": {"kept": {"w": 1, "l": 0, "win": 1.0, "units": 1.0}},
                         "under": {"all": {"w": 0, "l": 1, "win": 0.0, "units": -1.0}}}}
    card = {"assists_alone": _rec(5, 5, 0.0, 0.01),
            "assists_plus_rebound_overs": _rec(5, 5, 0.0, 0.0)}
    no = {"pretip": {"ranking_v2_by_side": {"over": {"all": _rec(40, 60, -10.0)},
                                            "under": {"all": _rec(55, 45, 8.0)}},
                     "card_top5": card}}
    assert RE.agreement(live, no).startswith("**The old seasons do not agree")
