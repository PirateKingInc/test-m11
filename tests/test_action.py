"""The Action card (owner 2026-11-12, betdata/action.py; MODEL.md "Action card")."""

from datetime import UTC, datetime, timedelta

import pytest

import tests.test_og_ranking as T
from betdata import action as AC
from betdata import eligibility as EL
from betdata.alerts import card as CD
from betdata.alerts.db import engine_for, get_state

NOW = datetime(2026, 11, 13, 11, 15, tzinfo=UTC)  # Fri Nov 13, 6:15 AM ET
TIP = datetime(2026, 11, 14, 0, 30, tzinfo=UTC)


@pytest.fixture
def pdb(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "NOW", NOW)
    monkeypatch.setattr(T, "TIP", TIP)
    monkeypatch.setattr(EL, "teammates_out", lambda m, g, t, pl, at: 2 if pl == "tm" else 0)
    monkeypatch.setattr(CD, "lines_exist", lambda *a: True)
    return engine_for(tmp_path / "poller.sqlite")


def _pick(p, key, **kw):
    T._pick(p, key, made=kw.pop("made", NOW - timedelta(hours=2)), **kw)


def _run(pdb):
    (og,) = CD.run_shadow(pdb, T._Main(), NOW, lambda m, s, d: TIP if s == "nba" else None)
    with pdb.connect() as p:
        return og, get_state(p, AC.key(NOW.date()))


def _keys(c):
    return [x["dedup_key"] for x in c["picks"]]


def test_pool_excludes_og_picks_and_their_player_game_market(pdb):
    with pdb.begin() as p:
        _pick(p, "og1", player="Zubac", fair=0.7)  # on OG (assists)
        _pick(p, "og1_other_line", player="Zubac", fair=0.69, line=3.5, median=5.0)  # same pgm
        _pick(p, "og1_fd", player="Zubac", fair=0.6, book="fanduel")  # same pgm, other book
        _pick(p, "zubac_reb", player="Zubac", market="rebounds", fair=0.65, line=9.5,
              median=10.5)  # same player, other market: allowed
    og, act = _run(pdb)
    assert _keys(og) == ["og1"]
    assert _keys(act) == ["zubac_reb"]
    assert act["picks"][0]["reasons"] == ["rebounds"]


def test_same_player_twice_in_action_in_different_markets_one_per_market(pdb):
    with pdb.begin() as p:
        _pick(p, "reb_a", player="Zubac", market="rebounds", fair=0.62, line=9.5, median=10.0)
        _pick(p, "reb_b", player="Zubac", market="rebounds", fair=0.66, line=8.5, median=10.0,
              book="fanduel")  # same pgm: the higher probability is kept
        _pick(p, "ast_u", player="Zubac", side="under", line=6.5, median=5.0, fair=0.64)
    og, act = _run(pdb)
    assert og["picks"] == []
    assert _keys(act) == ["reb_b", "ast_u"]
    assert [x["reasons"] for x in act["picks"]] == [["rebounds"], ["under 5.5+ assists"]]


def test_a_market_it_has_never_seen_joins(pdb):
    with pdb.begin() as p:
        _pick(p, "threes", player="Curry", market="threes", fair=0.6, line=4.5, median=5.0)
    _, act = _run(pdb)
    assert _keys(act) == ["threes"] and act["picks"][0]["reasons"] == ["non-assist market"]
    assert AC.pick_line(act["picks"][0]).startswith("Curry o4.5 3pm -110 · 0.25u")


def test_all_reject_kinds_and_below_top_5_with_ordered_reasons(pdb):
    with pdb.begin() as p:
        for i in range(5):  # OG's five
            _pick(p, f"og{i}", fair=0.80 - i / 100)
        _pick(p, "below", fair=0.60)  # eligible, sixth: below top 5
        _pick(p, "price", fair=0.75, price=150)  # Ranking v2: price
        _pick(p, "wrongmed", fair=0.74, median=4.0)  # Ranking v2: median
        _pick(p, "multi", fair=0.73, market="rebounds", price=200, median=3.0, line=8.5)
        _pick(p, "tm", fair=0.72, side="under", line=4.5, median=3.0)  # Rules v2: teammates
    og, act = _run(pdb)
    assert _keys(og) == [f"og{i}" for i in range(5)]
    reasons = {x["dedup_key"]: x["reasons"] for x in act["picks"]}
    assert _keys(act) == ["price", "wrongmed", "multi"]  # by probability, top 3
    assert reasons == {"price": ["outside price range"], "wrongmed": ["median wrong side"],
                       "multi": ["rebounds", "outside price range", "median wrong side"]}
    assert act["picks"][2]["reason"] == "rebounds"  # the first in Rules v2 -> Ranking v2 order
    assert act["reasons"] == {"outside price range": 1, "median wrong side": 1, "rebounds": 1,
                              "teammates out": 1, "ranked below top 5": 1}


def test_ranking_order_tie_breaks_stake_and_no_labels(pdb):
    with pdb.begin() as p:
        _pick(p, "a", fair=0.6, price=200, player="A")
        _pick(p, "b_long", fair=0.65, price=180, player="B")
        _pick(p, "b_short", fair=0.65, price=150, player="C")
        _pick(p, "early", fair=0.65, price=150, player="D", made=NOW - timedelta(hours=5))
    _, act = _run(pdb)
    assert _keys(act) == ["early", "b_short", "b_long"]
    assert all(x["stake"] == 0.25 and x["label"] == "" for x in act["picks"])


def test_fewer_than_3_and_empty(pdb, tmp_path):
    with pdb.begin() as p:
        _pick(p, "ok")
        _pick(p, "reb", market="rebounds", line=8.5, median=9.0)
    og, act = _run(pdb)
    assert _keys(og) == ["ok"] and _keys(act) == ["reb"]
    assert AC.note(act) == "Action: 1 pick — only 1 candidate off the OG card"
    pdb2 = engine_for(tmp_path / "p2.sqlite")
    with pdb2.begin() as p:
        _pick(p, "only")
    og2, act2 = _run(pdb2)
    assert _keys(og2) == ["only"] and act2["picks"] == []
    assert AC.note(act2) == "Action: no picks — every OG candidate is on the OG card"
    assert AC.push_lines(act2) == ["Action picks: none today"]


def test_not_before_nov_13(pdb):
    nov12 = NOW - timedelta(days=1)
    with pdb.begin() as p:
        _pick(p, "x", made=nov12 - timedelta(hours=1))
    CD.run_shadow(pdb, T._Main(), nov12, lambda m, s, d: TIP - timedelta(days=1))
    with pdb.connect() as p:
        assert get_state(p, AC.key(nov12.date())) is None


def test_ledger_rows_reference_existing_picks(pdb, tmp_path):
    from betdata.ledger import pick_id, read_rows

    with pdb.begin() as p:
        _pick(p, "reb", market="rebounds", line=8.5, median=9.0, player="Zubac")
    _run(pdb)
    for _ in range(2):
        with pdb.connect() as p:
            AC.ledger_rows(p, tmp_path, NOW)
    (r,) = read_rows(tmp_path / "action_card.csv")
    assert (r["pick_id"], r["action_stake"], r["reason"], r["market"]) == (
        pick_id("reb"), "0.25", "rebounds", "rebounds")
    (d,) = read_rows(tmp_path / "card_days.csv")
    assert (d["kind"], d["picks"]) == ("action", "1")


def test_official_cards_identical_with_and_without_action(tmp_path, monkeypatch):
    """Action is chosen after OG from OG's own card; OG and the Top 3 shadow are the same with
    Action on or off (Next Gen, Spread and the parlay never read it: pin test)."""
    from betdata import og_ranking as OR

    monkeypatch.setattr(T, "NOW", NOW)
    monkeypatch.setattr(T, "TIP", TIP)
    monkeypatch.setattr(EL, "teammates_out", lambda *a: 0)
    monkeypatch.setattr(CD, "lines_exist", lambda *a: True)
    states = []
    for on in (True, False):
        monkeypatch.setattr(AC, "active", lambda on=on: on)
        pdb = engine_for(tmp_path / f"{on}.sqlite")
        with pdb.begin() as p:
            for i in range(7):
                _pick(p, f"k{i}", fair=0.7 - i / 100, player=f"p{i}")
            _pick(p, "reb", market="rebounds", line=8.5, median=9.0)
        CD.run_shadow(pdb, T._Main(), NOW, lambda m, s, d: TIP)
        with pdb.connect() as p:
            states.append((get_state(p, CD.shadow_key("nba", NOW.date())),
                           get_state(p, OR.top3_key("nba", NOW.date())),
                           get_state(p, AC.key(NOW.date()))))
    (og1, t1, a1), (og2, t2, a2) = states
    assert og1 == og2 and t1 == t2
    assert a1 is not None and a2 is None


def test_push_section_with_picks_none_today_and_short(pdb):
    from betdata import morning as M

    with pdb.begin() as p:
        _pick(p, "og1", player="Zubac", fair=0.7)
        _pick(p, "zr", player="Ivica Zubac", market="rebounds", side="under", line=9.5,
              median=9.0, price=116, fair=0.6)
    _run(pdb)
    with pdb.connect() as p:
        _t, body = M.message(p, [], "nba", NOW)
    lines = body.split("\n")
    i = lines.index("Action picks (not official):")
    assert lines[i + 1] == "  Ivica Zubac u9.5 reb +116 · 0.25u · missed: rebounds"
    assert lines[i + 2] == "  1 pick — only 1 candidate off the OG card"
    assert i > max(j for j, x in enumerate(lines) if x.startswith("Spread"))  # after Spread
    assert AC.push_lines({"picks": []}) == ["Action picks: none today"]
    full = {"picks": [{"player": f"P{i}", "side": "over", "line": 4.5, "market": "assists",
                       "price": 110, "stake": 0.25, "reason": "rebounds"} for i in range(3)],
            "candidates": 5}
    assert len(AC.push_lines(full)) == 4  # a full card: no reason line
