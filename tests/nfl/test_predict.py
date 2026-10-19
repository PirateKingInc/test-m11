"""Live predictions: prediction point, factor labels, blend, display."""

from datetime import timedelta

import pandas as pd
import pytest

from betdata.nfl.groups import GROUPS
from betdata.nfl.pipeline import build, outcomes
from betdata.nfl.points import FINAL_REPORT, PREKICK, final_report_time, prekick_time
from betdata.nfl.predict import (
    EARLY,
    fmt_pick,
    group_factors,
    label,
    point_now,
    predict_games,
    version_of,
)
from betdata.nfl.selection import Design, fit_design
from tests.nfl.synth import make_store

DESIGN = Design(["base", "avail", "str"], ["base", "avail", "env"], 100.0, 30.0)


@pytest.fixture(scope="module")
def setup():
    from betdata.nfl.features.availability import learn_play_prob

    store = make_store()
    store = store.with_params(**learn_play_prob(store, [2018]))
    rows = build(store, [GROUPS[g] for g in DESIGN.groups], list(store.games.index))
    return store, rows, outcomes(store)


def test_point_now():
    k = pd.Timestamp("2024-10-06 17:00", tz="UTC")
    assert point_now(k, final_report_time(k) - timedelta(hours=1)) == EARLY
    assert point_now(k, final_report_time(k)) == FINAL_REPORT
    assert point_now(k, prekick_time(k)) == PREKICK


def test_labels_and_factor_grouping():
    assert label("m_avail_qb_change") == "QB change"
    assert label("m_avail_OL") == "OL availability"
    assert label("t_env_wind") == "wind"
    f = group_factors({"m_str_pass": 1.0, "m_str_rush": -0.5, "m_avail_qb_value": -3.0})
    assert f[0] == ("QB quality", -3.0)


def test_live_predictions_with_factors(setup):
    store, rows, out = setup
    fits = fit_design(rows, out, DESIGN, [2018, 2019])
    g = store.games
    gid = int(g[(g.season == 2020) & (g.week == 6)].index[0])
    now = prekick_time(g.at[gid, "kickoff"]) + timedelta(minutes=5)
    [pick] = predict_games(store, DESIGN, fits, [GROUPS[n] for n in DESIGN.groups], [gid], now,
                           {"margin": 0.4, "total": 0.15})  # fmt: skip
    assert pick.point == PREKICK and pick.market_spread is not None
    assert pick.blend_margin == pytest.approx(0.4 * pick.margin + 0.6 * -pick.market_spread,
                                              abs=0.02)  # fmt: skip
    text = "\n".join(fmt_pick(pick))
    assert "line factors" in text and "total factors" in text
    assert version_of(DESIGN, fits).startswith("m12:")


def test_predictions_are_stored_with_snapshot_change_only(engine, settings, fake_api, clock):
    from sqlalchemy import select

    from betdata.db.schema import game_source_ids, model_predictions
    from betdata.nfl.predict import LivePick, store_picks
    from betdata.sources.nflverse import NflverseSource
    from tests.conftest import fixture_bytes

    nfl = NflverseSource(engine, settings.source("nflverse"), transport=fake_api.transport,
                         sleep=lambda s: None, clock=clock)  # fmt: skip
    fake_api.add(body=fixture_bytes("nflverse", "games_sample.csv"))
    nfl.run("schedules", seasons="all")
    with engine.begin() as conn:
        gid = conn.execute(select(game_source_ids.c.game_id).where(
            game_source_ids.c.source_game_id == "2026_06_CHI_ATL")).scalar()  # fmt: skip
    pick = LivePick(gid, clock.now, "ATL", "CHI", PREKICK, -2.3, 48.2, 0.43, 1.5, 51.5, -1.5,
                    51.0, [("QB quality", -1.2)], [("roof", -0.6)], {"home": ["CB X (0%)"]})
    pick.features = {"m_avail_qb_value": -3.1}
    with engine.begin() as conn:
        store_picks(conn, [pick], "m12:test", clock.now)
        store_picks(conn, [pick], "m12:test", clock.now + timedelta(minutes=15))  # unchanged
        pick.margin = -3.0
        store_picks(conn, [pick], "m12:test", clock.now + timedelta(minutes=30))
        rows = conn.execute(select(model_predictions)).mappings().all()
    assert len(rows) == 2
    r = rows[0]
    assert (r["prediction_point"], r["blend_home_margin"], r["market_spread_home"]) == (
        PREKICK, -1.5, 1.5)  # fmt: skip
    assert r["features"] == {"m_avail_qb_value": -3.1}
    assert r["factors"]["margin"][0] == ["QB quality", -1.2]
    assert r["factors"]["missing"] == {"home": ["CB X (0%)"]}
