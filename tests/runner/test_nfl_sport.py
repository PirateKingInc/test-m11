"""The NFL plugin behind the sport interface: config and page cards."""

from datetime import timedelta

import pytest
from sqlalchemy import insert, select

from betdata.db.schema import game_source_ids, model_predictions
from betdata.sources.nflverse import NflverseSource
from betdata.sports import active_sports
from tests.conftest import fixture_bytes


def test_nfl_cards_from_stored_predictions_and_quotes(engine, settings, fake_api, clock):
    nfl = NflverseSource(engine, settings.source("nflverse"), transport=fake_api.transport,
                         sleep=lambda s: None, clock=clock)  # fmt: skip
    fake_api.add(body=fixture_bytes("nflverse", "games_sample.csv"))
    assert nfl.run("schedules", seasons="all").status == "ok"
    [sport] = active_sports()
    with engine.begin() as conn:
        gid = conn.execute(select(game_source_ids.c.game_id).where(
            game_source_ids.c.source_game_id == "2026_06_HOU_JAX")).scalar()  # fmt: skip
        conn.execute(insert(model_predictions).values(
            model=sport.page_model, model_version="m12:test", game_id=gid,
            predicted_at=clock.now - timedelta(minutes=5), pred_home_margin=7.0,
            pred_home_win_prob=0.70, pred_total=44.0, market_spread_home=-2.5,
            prediction_point="early", market_ml_home=-142, market_ml_away=120,
            factors={"margin": [["QB quality", 2.1], ["rest days", 1.0], ["travel", 0.5],
                                ["turf", 0.1]], "missing": {"home": ["QB A (0%)"]}}))
        # fmt: skip
        cards = {c.game_id: c for c in sport.cards(conn, clock.now)}
    c = cards[gid]
    assert (c.home, c.away, c.point) == ("JAX", "HOU", "early")
    assert c.model_spread_home == -7.0 and c.market_spread.line == -2.5
    assert c.spread_edge == 4.5 and c.spread_pick == "JAX"  # >= 3 points
    assert c.ml_home.price == -142 and c.market_win_home == pytest.approx(0.5627, abs=1e-3)
    assert c.ml_edge == pytest.approx(13.7, abs=0.1) and c.ml_pick == "JAX"
    assert [f[0] for f in c.factors] == ["QB quality", "rest days", "travel"]
    assert c.missing == {"JAX": ["QB A (0%)"], "HOU": []}
    # A game with no prediction yet still shows its market.
    others = [x for x in cards.values() if x.game_id != gid]
    assert others and all(x.point == "none" and x.market_spread is not None for x in others)
    # Before the prediction existed, the card has no model numbers (used for line movement).
    with engine.connect() as conn:
        early = {x.game_id: x for x in sport.cards(conn, clock.now - timedelta(minutes=10))}
    assert early[gid].model_spread_home is None


def test_sports_config(tmp_path):
    from betdata.sports import load_entries

    entries = {e.key: e for e in load_entries()}
    assert entries["nfl"].status == "active" and entries["nba"].status == "coming_soon"
    assert entries["nba"].plugin is None
    jobs = {j.id: j for j in entries["nfl"].jobs}
    assert jobs["nfl-odds-prekick"].when.sport == "nfl"  # gates on its own sport's games
    bad = tmp_path / "s.yaml"
    bad.write_text("sports: [{key: x, status: active}]")
    with pytest.raises(ValueError, match="names no plugin"):
        load_entries(bad)


def test_page_shows_the_weather_version_once_accepted():
    [sport] = active_sports()
    assert sport.page_model == "m13w"  # config/nfl_model_wx.json is committed
