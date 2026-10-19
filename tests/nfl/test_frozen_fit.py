"""Live NFL model: frozen fit (no hourly refit) and back-fill of missed prediction points."""

from datetime import timedelta

import pytest

from betdata.nfl import live
from betdata.nfl.groups import GROUPS
from betdata.nfl.pipeline import build, outcomes
from betdata.nfl.points import FINAL_REPORT, PREKICK, final_report_time, prekick_time
from betdata.nfl.predict import predict_games, version_of
from betdata.nfl.selection import Design, fit_design
from tests.nfl.synth import make_store

DESIGN = Design(["base", "avail", "str"], ["base", "avail", "env"], 100.0, 30.0)


@pytest.fixture(scope="module")
def synth():
    from betdata.nfl.features.availability import learn_play_prob

    store = make_store()
    store = store.with_params(**learn_play_prob(store, [2018]))
    rows = build(store, [GROUPS[g] for g in DESIGN.groups], list(store.games.index))
    return store, fit_design(rows, outcomes(store), DESIGN, [2018, 2019])


def test_frozen_fit_is_computed_once_and_read_back(synth, tmp_path, monkeypatch):
    store, fits = synth
    monkeypatch.setenv("BETDATA_DATA_DIR", str(tmp_path))
    calls = []

    def fake_live_model(store, design, now):
        calls.append(now)
        return fits, (2018, 2019)

    monkeypatch.setattr(live, "live_model", fake_live_model)
    g = store.games
    now = g.kickoff.max() + timedelta(days=200)
    first, _ = live.frozen_model(store, DESIGN, now)
    again, _ = live.frozen_model(store, DESIGN, now + timedelta(hours=1))
    assert len(calls) == 1  # the second (hourly) call reads the stored fit
    assert version_of(DESIGN, again) == version_of(DESIGN, fits)
    gid = int(g.index[-1])
    groups = [GROUPS[n] for n in DESIGN.groups]
    w = {"margin": 0.4, "total": 0.15}
    t = g.at[gid, "kickoff"] - timedelta(hours=1)
    [a] = predict_games(store, DESIGN, fits, groups, [gid], t, w)
    [b] = predict_games(store, DESIGN, again, groups, [gid], t, w)
    assert (a.margin, a.total, a.win_prob) == (b.margin, b.total, b.win_prob)


def test_missed_points(synth, engine):
    store, _ = synth
    g = store.games
    gid = int(g.index[-1])
    k = g.at[gid, "kickoff"]
    with engine.connect() as conn:
        # Between the final report and prekick: the current point needs no back-fill.
        assert live.missed_points(conn, store, "v", [gid], final_report_time(k)) == []
        # After prekick with nothing stored: the final-report point was missed.
        now = prekick_time(k) + timedelta(minutes=10)
        assert live.missed_points(conn, store, "v", [gid], now) == [(gid, FINAL_REPORT)]
    assert PREKICK != FINAL_REPORT
