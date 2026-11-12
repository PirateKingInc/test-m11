"""The OG + threes shadow (owner 2026-11-12, og_ranking.threes_shadow): OG's rules with made
threes eligible; morning only; never pushed, shown or official."""

import inspect

import pytest

import tests.test_og_ranking as T
from betdata import og_ranking as OR
from betdata.alerts import card as CD
from betdata.alerts.db import engine_for, get_state
from tests.test_action import NOW, TIP, _pick, _run


@pytest.fixture
def pdb(tmp_path, monkeypatch):
    from betdata import eligibility as EL

    monkeypatch.setattr(T, "NOW", NOW)
    monkeypatch.setattr(T, "TIP", TIP)
    monkeypatch.setattr(EL, "teammates_out", lambda *a: 0)
    monkeypatch.setattr(CD, "lines_exist", lambda *a: True)
    return engine_for(tmp_path / "poller.sqlite")


def test_threes_eligible_og_card_unchanged(pdb):
    with pdb.begin() as p:
        for i in range(4):
            _pick(p, f"a{i}", player=f"A{i}", fair=0.70 - i / 100)  # assists
        _pick(p, "t_top", player="S", market="threes", line=2.5, median=2.0, side="under",
              fair=0.75)  # threes: eligible in the shadow only
        _pick(p, "t_s_ast", player="S", fair=0.55)  # same player, assists: both allowed
        _pick(p, "t_long", player="L", market="threes", price=180, fair=0.9)  # price rule
        _pick(p, "t_med", player="M", market="threes", line=2.5, median=2.5, fair=0.9)  # median
        _pick(p, "reb", player="R", market="rebounds", side="under", line=8.5, median=8.0,
              fair=0.95)  # rebound unders: out of both (Rules v3)
        _pick(p, "u55", player="U", side="under", line=6.5, median=5.0, fair=0.95)
    og, _ = _run(pdb)
    with pdb.connect() as p:
        sh = get_state(p, OR.threes_key("nba", NOW.date()))
    assert [x["dedup_key"] for x in og["picks"]] == ["a0", "a1", "a2", "a3", "t_s_ast"]
    assert [x["dedup_key"] for x in sh["picks"]] == ["t_top", "a0", "a1", "a2", "a3"]
    assert [x["market"] for x in sh["picks"]][0] == "threes"
    assert all(x["stake"] == 0.5 for x in sh["picks"])
    assert sh["source"] == OR.THREES_SHADOW and sh["selected_at"] == og["selected_at"]


def test_same_player_threes_and_assists_both_on_the_shadow(pdb):
    with pdb.begin() as p:
        _pick(p, "s3", player="S", market="threes", line=2.5, median=3.0, fair=0.62)
        _pick(p, "sa", player="S", fair=0.61)
    _run(pdb)
    with pdb.connect() as p:
        sh = get_state(p, OR.threes_key("nba", NOW.date()))
    assert [x["dedup_key"] for x in sh["picks"]] == ["s3", "sa"]


def test_not_before_nov_13_and_ledger_rows(pdb, tmp_path):
    from datetime import timedelta

    from betdata.ledger import pick_id, read_rows

    nov12 = NOW - timedelta(days=1)
    with pdb.begin() as p:
        _pick(p, "old", made=nov12 - timedelta(hours=2))
    CD.run_shadow(pdb, T._Main(), nov12, lambda m, s, d: TIP - timedelta(days=1))
    with pdb.connect() as p:
        assert get_state(p, OR.threes_key("nba", nov12.date())) is None
    with pdb.begin() as p:
        _pick(p, "t", market="threes", line=2.5, median=3.0, fair=0.6)
    _run(pdb)
    with pdb.connect() as p:
        OR.threes_rows(p, tmp_path, NOW)
        OR.threes_rows(p, tmp_path, NOW)
    rows = read_rows(tmp_path / "og_threes_shadow.csv")  # written once
    assert [(r["pick_id"], r["market"], r["stake"]) for r in rows] == [
        (pick_id("old"), "assists", "0.5"), (pick_id("t"), "threes", "0.5")]
    (d,) = [x for x in read_rows(tmp_path / "card_days.csv") if x["kind"] == "ogthrees"]
    assert d["picks"] == "2"


def test_never_pushed_shown_or_official():
    from betdata import cards as C
    from betdata import morning as M
    from betdata import official as O
    from betdata.page import official as PO
    from betdata.page import tracker as PT

    for mod in (PO, PT, O):
        src = inspect.getsource(mod)
        assert "ogthrees" not in src and "og_threes" not in src
    body = inspect.getsource(M).split("def nba_message")[1].split("\ndef ")[0]
    assert "threes_key" not in body and "ogthrees" not in body
    assert "ogthrees" not in PO.nba_cards() and not C.active("ogthrees")


def _r(day, result, market="assists", odds="-110"):
    return {"day": day, "result": result, "profit": "0.9091" if result == "win" else "-1.0",
            "card_stake": "0.5", "odds": odds, "market": market, "clv_consensus": ""}


def test_earn_in_bar(monkeypatch, tmp_path):
    from betdata import og_review as RV

    days = ["2026-11-13"]
    monkeypatch.setattr(RV, "card_days", lambda out, kind: days)
    off = [_r("2026-11-13", "win")] * 5 + [_r("2026-11-13", "loss")] * 5
    monkeypatch.setattr(RV, "official_rows", lambda rows: off)
    few = [_r("2026-11-13", "win", "threes")] * 12
    monkeypatch.setattr(RV, "threes_shadow_rows", lambda out: few)
    t = "\n".join(RV.earn_in([], tmp_path))
    assert "Not enough yet" in t and "12 of 30" in t
    met = [_r("2026-11-13", "win", "threes")] * 18 + [_r("2026-11-13", "loss", "threes")] * 12
    monkeypatch.setattr(RV, "threes_shadow_rows", lambda out: met)
    assert "Threes met the bar" in "\n".join(RV.earn_in([], tmp_path))
    bad = [_r("2026-11-13", "win", "threes")] * 14 + [_r("2026-11-13", "loss", "threes")] * 16
    monkeypatch.setattr(RV, "threes_shadow_rows", lambda out: bad)
    t = "\n".join(RV.earn_in([], tmp_path))
    assert "did not meet the bar" in t and "below .500" in t
    monkeypatch.setattr(RV, "card_days", lambda out, kind: [])
    assert "No OG + threes shadow card yet" in RV.earn_in([], tmp_path)[0]
