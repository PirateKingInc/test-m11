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
    sport = next(s for s in active_sports() if s.key == "nfl")
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
        # Lines are DraftKings/FanDuel only, best of the two (nfl/book_lines.py): JAX -2.5 at
        # DK, -2.0 at FD -> the card shows FD's -2 (the better number for the JAX pick).
        at = clock.now - timedelta(minutes=30)
        for book, home_line, ml in (("draftkings", -2.5, (-142, 120)),
                                    ("fanduel", -2.0, (-150, 125))):
            for outcome, line in (("home", home_line), ("away", -home_line)):
                _quote(conn, gid, book, "spreads", outcome, line, -110, at)
            _quote(conn, gid, book, "h2h", "home", None, ml[0], at)
            _quote(conn, gid, book, "h2h", "away", None, ml[1], at)
        other = conn.execute(select(game_source_ids.c.game_id).where(
            game_source_ids.c.source_game_id == "2026_06_CHI_ATL")).scalar()  # fmt: skip
        _quote(conn, other, "draftkings", "spreads", "home", -3.0, -110, at)
        cards = {c.game_id: c for c in sport.cards(conn, clock.now)}
    c = cards[gid]
    assert (c.home, c.away, c.point) == ("JAX", "HOU", "early")
    assert c.model_spread_home == -7.0
    assert (c.market_spread.line, c.market_spread.book) == (-2.0, "fanduel")
    assert c.spread_edge == 5.0 and c.spread_pick == "JAX"  # >= 3 points
    # moneyline: DK's -142 gives JAX the larger edge than FD's -150
    assert c.ml_home.book == "draftkings" and c.ml_home.price == -142
    assert c.market_win_home == pytest.approx(0.5627, abs=1e-3)
    assert c.ml_edge == pytest.approx(13.7, abs=0.1) and c.ml_pick == "JAX"
    assert [f[0] for f in c.factors] == ["QB quality", "rest days", "travel"]
    assert c.missing == {"JAX": ["QB A (0%)"], "HOU": []}
    # A game with no prediction yet still shows its DK/FD market; nflverse's line is not shown.
    assert cards[other].point == "none" and cards[other].market_spread.line == -3.0
    assert all(x.market_spread is None for x in cards.values() if x.game_id not in (gid, other))
    # Before the prediction existed, the card has no model numbers (used for line movement).
    with engine.connect() as conn:
        early = {x.game_id: x for x in sport.cards(conn, clock.now - timedelta(minutes=10))}
    assert early[gid].model_spread_home is None


def test_sports_config(tmp_path):
    from betdata.sports import load_entries

    entries = {e.key: e for e in load_entries()}
    assert entries["nfl"].status == "active" and entries["nba"].status == "active"
    assert entries["nba"].plugin == "betdata.sports.nba:NbaSport"
    jobs = {j.id: j for j in entries["nfl"].jobs}
    assert jobs["nfl-odds-prekick"].when.sport == "nfl"  # gates on its own sport's games
    bad = tmp_path / "s.yaml"
    bad.write_text("sports: [{key: x, status: active}]")
    with pytest.raises(ValueError, match="names no plugin"):
        load_entries(bad)


def test_page_shows_the_weather_version_once_accepted():
    sport = next(s for s in active_sports() if s.key == "nfl")
    assert sport.page_model == "m13w"  # config/nfl_model_wx.json is committed


def _quote(conn, game, book, market, outcome, line, price, at):
    from betdata.db.schema import books, markets, odds_snapshots

    bid = conn.execute(select(books.c.book_id).where(books.c.key == book)).scalar()
    mid = conn.execute(select(markets.c.market_id).where(
        markets.c.key == market, markets.c.period == "game")).scalar()  # fmt: skip
    conn.execute(insert(odds_snapshots).values(
        source="sharpapi", source_game_id=f"t{game}", game_id=game, book_id=bid, market_id=mid,
        outcome=outcome, line=line, price_american=price, fetched_at=at))  # fmt: skip
