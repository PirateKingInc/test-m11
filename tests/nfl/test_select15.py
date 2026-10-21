"""`nfl select15` on the synthetic league: steps, kept groups, ablation and the draft design."""

import json

from betdata.nfl.features.availability import learn_play_prob
from betdata.nfl.groups import GROUPS
from betdata.nfl.pipeline import build, outcomes
from betdata.nfl.select15 import select15, write
from betdata.nfl.selection import Design
from tests.nfl.synth15 import make_store15

START = Design(["base", "avail"], ["base", "env"], 100.0, 30.0)


def test_select15_adds_groups_in_order_and_writes_a_draft(tmp_path, monkeypatch):
    import betdata.nfl.select15 as s15

    monkeypatch.setattr(s15, "TRAIN", (2018, 2019))
    monkeypatch.setattr(s15, "VALIDATE", (2020,))
    monkeypatch.setattr(s15, "_variants", lambda: {})
    store = make_store15()
    store = store.with_params(**learn_play_prob(store, [2018]))
    ids = list(store.games.index)
    base = build(store, [GROUPS[g] for g in START.groups], ids)
    rep = select15(base, outcomes(store), lambda g: build(store, [g], ids), START,
                   log=lambda *_: None)  # fmt: skip
    assert [s.get("group") for s in rep["steps"][1:]] == ["lines", "depth", "st", "sitluck",
                                                          "pen"]  # fmt: skip
    kept = [g for g in ("lines", "depth", "st", "sitluck", "pen")
            if g in rep["design"]["margin_groups"] + rep["design"]["total_groups"]]  # fmt: skip
    assert [a["without"] for a in rep["ablation"][1:]] == kept
    assert set(rep["group_versions"]) == set(kept)
    write(rep, {"x": 1}, tmp_path / "r.json", tmp_path / "d.json")
    d = json.loads((tmp_path / "d.json").read_text())
    assert d["margin_groups"][:2] == ["base", "avail"] and "status" in d
