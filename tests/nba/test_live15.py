"""M15 nba15 live: predictions stored as model nba15 beside M14 (change-only), and the page
cards read nba15 only when the switch rule set `page: true`, falling back to M14's prediction
for a game nba15 has not predicted."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from betdata.db.schema import games, model_predictions
from betdata.nba import live15 as L
from betdata.nba.data import load_store, outcomes
from betdata.nba.features import GROUPS
from betdata.nba.features15 import GROUPS15
from betdata.nba.pipeline import build, columns
from betdata.nba.selection import fits_for
from tests.nba.synth import synth

GROUPS_USED = ["avail", "rapm", "luck3", "load"]


@pytest.fixture
def setup(engine, tmp_path, monkeypatch):
    ids = synth(engine, n_days=40)
    with engine.connect() as conn:
        store = load_store(conn)
    store = store.with_params(load_sit={"000": 0.03}, rapm={})
    rows = build(store, [GROUPS["base"], *((GROUPS | GROUPS15)[n] for n in GROUPS_USED)],
                 ids[:35])  # fmt: skip
    m, t = columns(rows, GROUPS_USED)
    fits = fits_for(rows, outcomes(store), m, t, (2021,), 10.0, 10.0)
    fit = tmp_path / "fit15.json"
    fit.write_text(json.dumps({"fits": {p: {"margin": f.margin.to_dict(),
                                            "total": f.total.to_dict(), "sigma": f.sigma}
                                        for p, f in fits.items()},
                               "load_sit": {"000": 0.03}, "play_prob": {}}))  # fmt: skip
    design = tmp_path / "nba15.json"
    design.write_text(json.dumps({"groups": GROUPS_USED, "lam_m": 10, "lam_t": 10,
                                  "fit": str(fit), "page": False}))  # fmt: skip
    monkeypatch.setattr(L, "DESIGN_PATH", design)
    monkeypatch.setattr(L, "PROJECT_ROOT", tmp_path)
    gid = ids[-1]
    with engine.begin() as conn:
        conn.execute(update(games).where(games.c.game_id == gid).values(status="scheduled"))
        tip = conn.execute(select(games.c.start_time_utc).where(games.c.game_id == gid)).scalar()
    return gid, tip, design


def test_background_then_page(setup, engine):
    gid, tip, design = setup
    now = tip - timedelta(hours=2)
    picks = L.run_predict15(engine, now)
    assert [p.game_id for p in picks] == [gid]
    assert L.run_predict15(engine, now + timedelta(minutes=1))  # recomputed...
    with engine.connect() as conn:
        rows = conn.execute(select(model_predictions.c.model, model_predictions.c.model_version)
                            .where(model_predictions.c.game_id == gid)).all()  # fmt: skip
    assert {r.model for r in rows} == {"nba15"} and len(rows) == 1  # ...but stored once
    assert rows[0].model_version.startswith("nba15:")
    assert L.Design15.load().page is False
    d = json.loads(design.read_text())
    design.write_text(json.dumps({**d, "page": True}))
    assert L.Design15.load().page is True


def test_cards_prefer_page_version_with_fallback(setup, engine, monkeypatch):
    from sqlalchemy import insert

    from betdata.sports import load_entries
    from betdata.sports.nba import NbaSport

    gid, tip, _ = setup
    now = tip - timedelta(hours=2)
    sport = NbaSport(next(e for e in load_entries() if e.key == "nba"))
    with engine.begin() as conn:
        conn.execute(insert(model_predictions).values(
            model="nba", model_version="nba:x", game_id=gid, predicted_at=now,
            pred_home_margin=4.0, pred_home_win_prob=0.6, prediction_point="morning"))
    monkeypatch.setattr(NbaSport, "_page_model", lambda self: "nba15")
    with engine.connect() as conn:
        assert sport.cards(conn, now + timedelta(minutes=1))[0].model_spread_home == -4.0
    with engine.begin() as conn:
        conn.execute(insert(model_predictions).values(
            model="nba15", model_version="nba15:x", game_id=gid, predicted_at=now,
            pred_home_margin=7.0, pred_home_win_prob=0.7, prediction_point="morning"))
    with engine.connect() as conn:
        assert sport.cards(conn, now + timedelta(minutes=1))[0].model_spread_home == -7.0
