"""m15 live: the frozen fit (never overwritten), background predictions stored change-only as
model m15 and picked up by grading, and the page switch with per-game fallback to m13w."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from betdata.db.schema import game_source_ids, model_predictions
from betdata.nfl import live as nfl_live
from betdata.nfl import live15, m15data
from betdata.nfl.features.availability import learn_play_prob
from betdata.nfl.m15data import Expected, Logistic
from betdata.nfl.predict import LivePick, store_picks
from betdata.sources.nflverse import NflverseSource
from betdata.sports import active_sports
from tests.conftest import fixture_bytes
from tests.nfl.synth15 import make_store15

DESIGN = {"margin_groups": ["base", "avail", "depth", "st"],
          "total_groups": ["base", "env", "sitluck"], "lam_margin": 100.0, "lam_total": 30.0,
          "m15_group_versions": {"depth": {"version": 101}, "st": {"version": 1},
                                 "sitluck": {"version": 1}}, "page": False}  # fmt: skip
EXPECTED = Expected(Logistic(6.0, -0.1), Logistic(0.9, -0.8), Logistic(1.4, -0.08), 1.1, 1.2)


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    design = tmp_path / "m15.json"
    design.write_text(json.dumps(DESIGN))
    monkeypatch.setattr(live15, "DESIGN15_PATH", design)
    store = make_store15()
    store = store.with_params(**learn_play_prob(store, [2018]))
    now = datetime(2021, 1, 4, tzinfo=UTC)  # 2020 season: fit on 2013-2019 (synth: 2018-19)
    path = live15.freeze(store, {"expected": EXPECTED.to_dict()}, now, fits_dir=tmp_path)
    return store, design, path


def test_freeze_writes_a_new_fit_once(frozen, tmp_path):
    store, design, path = frozen
    raw = json.loads(path.read_text())
    assert raw["seasons"][-1] == 2019 and set(raw["fits"]) == {"final_report", "prekick"}
    assert Expected.from_dict(raw["expected"]) == EXPECTED
    assert json.loads(design.read_text())["fit"] == str(path)
    with pytest.raises(FileExistsError):
        live15.freeze(store, {"expected": {}}, datetime(2021, 1, 4, tzinfo=UTC),
                      fits_dir=tmp_path)  # fmt: skip
    got = live15.load15()
    assert got[4].startswith("m15:") and live15.page15() is False


def test_run_predict15_predicts_upcoming_games_as_m15(frozen, engine, monkeypatch):
    store, _, _ = frozen
    g = store.games.copy()
    last = g.index[-4:]
    g.loc[last, "status"] = "scheduled"
    store = type(store)(g, store.frames, store.params, names=store.names)
    seen = {}
    monkeypatch.setattr(live15, "MIN_LAST_SEASON_ROWS", 50)  # the synthetic league is small
    monkeypatch.setattr(nfl_live, "loaded_store", lambda engine: store)
    monkeypatch.setattr(m15data, "with_m15", lambda s, *a, **k: (s, {"exp": k["expected"]}))
    monkeypatch.setattr(nfl_live, "missed_points", lambda *a: [])
    monkeypatch.setattr(nfl_live, "attach_moneylines", lambda *a: None)
    monkeypatch.setattr(live15, "store_picks", lambda conn, picks, ver, t, model:
                        seen.update(ver=ver, model=model, ids=[p.game_id for p in picks]))
    now = g.at[last[0], "kickoff"] - timedelta(hours=30)
    picks = live15.run_predict15(engine, now, days=8)
    assert seen["model"] == "m15" and seen["ver"].startswith("m15:")
    assert set(seen["ids"]) == {int(i) for i in last} == {p.game_id for p in picks}
    assert any(lab.startswith(("kicker", "punting", "returns", "kickoff", "depth"))
               for p in picks for lab, _ in p.margin_factors)


def test_incomplete_inputs_are_refused(frozen):
    from betdata.nfl.store import Frame, Store

    store, _, _ = frozen
    now = store.games.kickoff.iloc[-1]
    tg = store.frame("m15_tg").df
    frames = {**store.frames, "m15_tg": Frame(tg[tg.season != 2020])}
    with pytest.raises(ValueError, match="missing"):
        live15.inputs_ready(Store(store.games, frames), now)  # last season's files absent


@pytest.fixture
def nfl_games(engine, settings, fake_api, clock):
    nfl = NflverseSource(engine, settings.source("nflverse"), transport=fake_api.transport,
                         sleep=lambda s: None, clock=clock)  # fmt: skip
    fake_api.add(body=fixture_bytes("nflverse", "games_sample.csv"))
    assert nfl.run("schedules", seasons="all").status == "ok"
    with engine.connect() as conn:
        return conn.execute(select(game_source_ids.c.game_id).where(
            game_source_ids.c.source_game_id == "2026_06_HOU_JAX")).scalar()  # fmt: skip


def test_m15_rows_are_change_only_and_graded(engine, nfl_games, clock):
    from betdata.nfl.grade import latest_predictions

    gid = nfl_games
    pick = LivePick(gid, clock.now + timedelta(days=1), "JAX", "HOU", "early", 3.0, 44.0, 0.6,
                    -2.5, 44.5, 0.5, 44.3)  # fmt: skip
    with engine.begin() as conn:
        store_picks(conn, [pick], "m15:x", clock.now, model="m15")
        store_picks(conn, [pick], "m15:x", clock.now + timedelta(hours=1), model="m15")
        rows = conn.execute(select(model_predictions.c.model, model_predictions.c.params)).all()
        assert [r.model for r in rows] == ["m15"]
        assert rows[0].params == {"design": "config/nfl_model_m15.json"}
        assert [p["model_version"] for p in latest_predictions(conn)] == ["m15:x"]


def test_page_switch_and_fallback_to_m13w(engine, nfl_games, clock, monkeypatch):
    gid = nfl_games
    sport = next(s for s in active_sports() if s.key == "nfl")
    monkeypatch.setattr(live15, "version15", lambda: "m15:abc")
    monkeypatch.setattr(live15, "page15", lambda: False)
    assert sport.page_model == "m13w" and sport.page_version() == "m13w"
    assert sport.background_versions() == ["m15:abc"]
    monkeypatch.setattr(live15, "page15", lambda: True)
    assert sport.page_model == "m15" and sport.page_version() == "m15:abc"
    assert sport.background_versions() == ["m13w"]

    def add(model, margin):
        with engine.begin() as conn:
            conn.execute(insert(model_predictions).values(
                model=model, model_version=f"{model}:t", game_id=gid,
                predicted_at=clock.now - timedelta(minutes=5), pred_home_margin=margin,
                pred_home_win_prob=0.6, pred_total=44.0, prediction_point="early"))  # fmt: skip

    def card():
        with engine.connect() as conn:
            return {c.game_id: c for c in sport.cards(conn, clock.now)}[gid]

    add("m13w", 4.0)
    assert card().model_spread_home == -4.0  # m15 has nothing for the game yet: m13w
    add("m15", 7.0)
    assert card().model_spread_home == -7.0
    monkeypatch.setattr(live15, "page15", lambda: False)
    assert card().model_spread_home == -4.0
