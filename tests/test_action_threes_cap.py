"""The Action threes cap (owner 2026-11-14, betdata/action.py): at most 1 threes pick a card day
across the morning card and live adds; the OG + threes shadow is not capped."""

from datetime import date, timedelta

import pytest

import tests.test_og_ranking as T
from betdata import action as AC
from betdata import og_ranking as OR
from betdata.alerts.db import get_state
from tests.test_action import NOW, _pick, _run
from tests.test_action_adds import LATER, Client, _later, pdb  # noqa: F401

T3 = {"market": "threes", "side": "under", "line": 1.5, "median": 1.0}


@pytest.fixture(autouse=True)
def cap_from_test_day(monkeypatch):
    monkeypatch.setattr(AC, "THREES_CAP_START", NOW.date())


def _og5(p):
    for i in range(5):
        _pick(p, f"og{i}", player=f"O{i}", fair=0.80 - i / 100)


def test_second_threes_skipped_and_replaced(pdb):  # noqa: F811
    with pdb.begin() as p:
        _og5(p)
        _pick(p, "t1", player="S1", fair=0.92, **T3)  # above OG's five: on the shadow too
        _pick(p, "t2", player="S2", fair=0.91, **T3)
        _pick(p, "t3", player="S3", fair=0.90, **T3)
        _pick(p, "reb", player="R", market="rebounds", side="under", line=8.5, median=8.0,
              fair=0.60)  # fmt: skip
        _pick(p, "ast", player="A", fair=0.55, price=150)  # outside OG's price range
    og, ac = _run(pdb)
    assert [x["dedup_key"] for x in ac["picks"]] == ["t1", "reb", "ast"]
    assert ac["capped"] == {"t2": AC.CAPPED, "t3": AC.CAPPED}
    with pdb.connect() as p:  # the OG + threes shadow is not capped
        sh = get_state(p, OR.threes_key("nba", NOW.date()))
    assert sum(x["market"] == "threes" for x in sh["picks"]) == 3


def test_cap_spans_the_morning_card_and_live_adds(pdb, tmp_path):  # noqa: F811
    from betdata.ledger import pick_id, read_rows

    with pdb.begin() as p:
        _og5(p)
        _pick(p, "t1", player="S1", fair=0.70, **T3)  # the morning card's one threes pick
    _run(pdb)
    with pdb.begin() as p:
        _later(p, "late3", player="L3", fair=0.95, **T3)  # would beat the morning 3rd
        _later(p, "late_ast", player="LA", fair=0.78)  # OG-quality assists
    adds = AC.live_adds(pdb, T._Main(), LATER, "topic", Client())
    assert [x["dedup_key"] for x in adds] == ["late_ast"]
    with pdb.connect() as p:
        st = get_state(p, AC.adds_key(NOW.date()))
        assert st["log"][-1]["why_not"].get(AC.CAPPED) == 1
        assert st["log"][-1]["capped"] == ["late3"]
        AC.cap_annotations(p, tmp_path, LATER)
        AC.cap_annotations(p, tmp_path, LATER)  # written once
    (r,) = read_rows(tmp_path / "annotations.csv")
    assert (r["pick_id"], r["field"], r["value"]) == (pick_id("late3"), "action_cap", AC.CAPPED)


def test_no_cap_before_its_start(pdb, monkeypatch):  # noqa: F811
    monkeypatch.setattr(AC, "THREES_CAP_START", date(2026, 11, 15))
    with pdb.begin() as p:
        _og5(p)
        for i in range(3):
            _pick(p, f"t{i}", player=f"S{i}", fair=0.70 - i / 100, **T3)
    _og, ac = _run(pdb)
    assert [x["dedup_key"] for x in ac["picks"]] == ["t0", "t1", "t2"]  # Nov 13: unchanged
    assert "capped" not in ac
    assert NOW.date() + timedelta(days=2) == AC.THREES_CAP_START
