"""m15's design file, the single test scoring and the page switch rule (synthetic league)."""

import json
from types import SimpleNamespace

import pytest

from betdata.moneyline import Calibration, MoneylineConfig
from betdata.nfl import m15
from betdata.nfl.features.availability import learn_play_prob
from betdata.nfl.pipeline import outcomes
from betdata.nfl.selection import Design
from tests.nfl.synth15 import make_store15

D13 = Design(["base", "avail"], ["base", "env"], 100.0, 30.0)
D15 = Design(["base", "avail", "depth", "st"], ["base", "env", "sitluck"], 100.0, 30.0)
META = {"m15_group_versions": {"depth": {"version": 101}, "st": {"version": 1},
                               "sitluck": {"version": 1}}}


def test_design_file_loads_and_keeps_its_extra_keys(tmp_path):
    p = tmp_path / "m15.json"
    p.write_text(json.dumps({**D15.__dict__, **META, "status": "draft"}))
    d, meta = m15.load_design15(p)
    assert d == D15 and meta["status"] == "draft"
    m15.save_meta({"page": False}, p)
    assert m15.load_design15(p)[1]["page"] is False
    names = [g.name for g in m15.groups_of(d, meta)]
    assert names == ["base", "avail", "depth", "st", "env", "sitluck"]
    assert m15.groups_of(d, meta)[2].version == 101
    with pytest.raises(ValueError):
        m15.m15_group("depth", 77)


def report(m_fr, m_pk):
    return {"points": {"final_report": {"margin_mae": {"m15": m_fr}},
                       "prekick": {"margin_mae": {"m15": m_pk}}}}


@pytest.mark.parametrize("valid, fr, pk, switch", [
    (9.778, 10.05, 9.99, True),
    (9.778, 10.057, 9.992, True),  # equal is "not worse"
    (9.778, 10.06, 9.90, False),  # worse at the final report
    (9.83, 10.0, 9.9, False),  # validation not improved
    (None, 10.0, 9.9, False),
])  # fmt: skip
def test_page_switch_rule(valid, fr, pk, switch):
    assert m15.page_switch(report(fr, pk), valid)["switch"] is switch


def test_final_test15_scores_once(tmp_path, monkeypatch):
    monkeypatch.setattr(m15, "TRAIN", (2018,))
    monkeypatch.setattr(m15, "VALIDATE", (2019,))
    monkeypatch.setattr(m15, "TEST", (2020,))
    store = make_store15()
    store = store.with_params(**learn_play_prob(store, [2018]))
    from betdata.nfl.pipeline import build

    rows = build(store, m15.groups_of(D15, META), list(store.games.index))
    out = outcomes(store)
    games = {i: SimpleNamespace(spread_home=-3.0, total=44.0, home_score=int(out.at[i, "margin"])
                                + 20, away_score=20) for i in out.index}  # fmt: skip
    ml = {i: (-150, 130) for i in out.index}
    cfg = MoneylineConfig(Calibration(1.0, 0.0, "t", False), 3.0)
    path = tmp_path / "ft.json"
    rep = m15.final_test15(rows, out, D15, D13, games, ml, cfg, path, validation_margin=9.7)
    assert rep["label"].startswith("third look")
    for r in rep["points"].values():
        assert set(r["margin_mae"]) == {"m15", "m13w", "m13w_recorded"}
        assert r["with_close"]["games"] == r["games"] > 0
        assert set(r["spread_bets"]) == set(r["moneyline"]) == {"m15", "m13w"}
    assert "switch" in rep["page"] and path.exists()
    with pytest.raises(RuntimeError):
        m15.final_test15(rows, out, D15, D13, games, ml, cfg, path)
