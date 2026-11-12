"""Action live adds (owner 2026-11-12, betdata/action.py): picks posted after the morning card."""

from datetime import timedelta

import pytest

import tests.test_og_ranking as T
from betdata import action as AC
from betdata import eligibility as EL
from betdata.alerts import card as CD
from betdata.alerts.db import engine_for, get_state
from tests.test_action import NOW, TIP, _pick, _run

LATER = NOW + timedelta(hours=2)  # 8:15 AM ET


@pytest.fixture
def pdb(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "NOW", NOW)
    monkeypatch.setattr(T, "TIP", TIP)
    monkeypatch.setattr(EL, "teammates_out", lambda *a: 0)
    monkeypatch.setattr(CD, "lines_exist", lambda *a: True)
    return engine_for(tmp_path / "poller.sqlite")


class Client:
    def __init__(self):
        self.sent = []

    def post(self, url, content, headers):
        self.sent.append((headers["Title"], content.decode()))
        return type("R", (), {"status_code": 200})()


def _morning(pdb):
    with pdb.begin() as p:
        for i in range(5):  # OG's five: 0.80 .. 0.76
            _pick(p, f"og{i}", player=f"O{i}", fair=0.80 - i / 100)
        for i in range(3):  # the morning Action card: rebounds 0.70, 0.69, 0.68
            _pick(p, f"ac{i}", player=f"A{i}", market="rebounds", line=8.5, median=9.0,
                  fair=0.70 - i / 100)
    og, ac = _run(pdb)
    assert len(og["picks"]) == 5 and [x["dedup_key"] for x in ac["picks"]] == ["ac0", "ac1",
                                                                               "ac2"]


def _later(p, key, **kw):
    _pick(p, key, made=LATER - timedelta(minutes=10), **kw)


def _adds(pdb, now=LATER, client=None):
    c = client or Client()
    return AC.live_adds(pdb, T._Main(), now, "topic", c), c


def test_og_quality_and_beats_the_morning_third(pdb):
    _morning(pdb)
    with pdb.begin() as p:
        _later(p, "ogq", player="Q", fair=0.78)  # assists, passes every rule, >= OG's 5th
        _later(p, "beat", player="B", market="rebounds", line=8.5, median=9.0, fair=0.685)
        _later(p, "weak", player="W", market="rebounds", line=8.5, median=9.0, fair=0.60)
    new, c = _adds(pdb)
    assert [(x["dedup_key"], x["og_quality"]) for x in new] == [("ogq", True), ("beat", False)]
    assert new[0]["note"] == AC.OG_NOTE and new[0]["reason"] == AC.POSTED_AFTER
    assert new[1]["reason"] == "rebounds" and new[0]["stake"] == 0.25
    assert new[0]["posted"] == "live 8:15 AM ET"
    assert [t for t, _ in c.sent] == ["Action add (not official)"] * 2
    assert c.sent[0][1].endswith("· 0.25u · missed: posted after the morning card · "
                                 + AC.OG_NOTE)
    assert AC.OG_NOTE not in c.sent[1][1]


def test_rejections_tip_soon_stale_same_pgm_and_other_market(pdb):
    _morning(pdb)
    with pdb.begin() as p:
        _later(p, "stale", player="S", fair=0.79, live=False)  # price no longer Live
        _later(p, "same", player="O0", fair=0.79, line=3.5, median=5.0)  # O0 assists on OG
        _later(p, "other", player="O0", market="rebounds", line=8.5, median=9.0,
               fair=0.69)  # O0 in another market: allowed
    new, _ = _adds(pdb)
    assert [x["dedup_key"] for x in new] == ["other"]
    log = get_state_log(pdb)
    assert log["why_not"] == {"price no longer Live": 1, "already on OG / Action": 1}
    # a pick whose game tips within 30 minutes is rejected
    with pdb.begin() as p:
        _pick(p, "soon", player="Z", fair=0.9, made=TIP - timedelta(minutes=40))
    new2, _ = _adds(pdb, now=TIP - timedelta(minutes=25))
    assert new2 == [] and get_state_log(pdb)["why_not"] == {"tips within 30 minutes": 1}


def get_state_log(pdb):
    with pdb.connect() as p:
        return get_state(p, AC.adds_key(NOW.date()))["log"][-1]


def test_daily_cap_og_quality_first_and_no_swaps(pdb):
    _morning(pdb)
    with pdb.begin() as p:
        _later(p, "b1", player="B1", market="rebounds", line=8.5, median=9.0, fair=0.95)
        _later(p, "b2", player="B2", market="rebounds", line=8.5, median=9.0, fair=0.94)
        _later(p, "q1", player="Q1", fair=0.77)  # OG-quality, lower probability: first
        _later(p, "b3", player="B3", market="rebounds", line=8.5, median=9.0, fair=0.93)
    new, _ = _adds(pdb)
    assert [x["dedup_key"] for x in new] == ["q1", "b1", "b2"]  # cap 3, OG-quality first
    assert get_state_log(pdb)["why_not"] == {"daily cap reached": 1}
    with pdb.begin() as p:  # a later, better pick never replaces an add
        _pick(p, "better", player="X", fair=0.99, made=LATER + timedelta(minutes=30))
    new2, _ = _adds(pdb, now=LATER + timedelta(hours=1))
    assert new2 == []
    with pdb.connect() as p:
        st = get_state(p, AC.adds_key(NOW.date()))
    assert [x["dedup_key"] for x in st["adds"]] == ["q1", "b1", "b2"]


def test_each_pick_is_judged_once(pdb):
    _morning(pdb)
    with pdb.begin() as p:
        _later(p, "once", player="X", market="rebounds", line=8.5, median=9.0, fair=0.69)
    assert len(_adds(pdb)[0]) == 1
    assert _adds(pdb, now=LATER + timedelta(minutes=5))[0] == []  # not seen again


def test_quiet_hours_and_before_the_morning_card(pdb):
    _morning(pdb)
    with pdb.begin() as p:
        _later(p, "late", player="L", fair=0.79)
    late = NOW + timedelta(hours=17, minutes=15)  # 11:30 PM ET
    assert _adds(pdb, now=late)[0] == []
    with pdb.connect() as p:
        assert get_state(p, AC.adds_key(NOW.date())) is None  # nothing evaluated
    early = NOW - timedelta(minutes=30)  # 5:45 AM ET: before 6 AM (and before the card)
    assert _adds(pdb, now=early)[0] == []


def test_ledger_rows_for_adds(pdb, tmp_path):
    from betdata.ledger import pick_id, read_rows

    _morning(pdb)
    with pdb.begin() as p:
        _later(p, "ogq", player="Q", fair=0.78)
    _adds(pdb)
    for _ in range(2):
        with pdb.connect() as p:
            AC.adds_ledger_rows(p, tmp_path, NOW)
    (r,) = read_rows(tmp_path / "action_adds.csv")
    assert (r["pick_id"], r["og_quality"], r["posted"], r["stake"], r["push"]) == (
        pick_id("ogq"), "true", "live 8:15 AM ET", "0.25", "http 200")


@pytest.mark.parametrize("og_n", [3])
def test_og_short_means_rules_alone_make_og_quality(pdb, og_n):
    with pdb.begin() as p:
        for i in range(og_n):
            _pick(p, f"og{i}", player=f"O{i}", fair=0.80 - i / 100)
    _run(pdb)
    with pdb.begin() as p:
        _later(p, "low", player="L", fair=0.52)  # passes the rules; OG had fewer than 5
    new, _ = _adds(pdb)
    assert [(x["dedup_key"], x["og_quality"]) for x in new] == [("low", True)]
