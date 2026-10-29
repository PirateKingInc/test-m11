"""M14 NBA plugin behind the M13 sport interface: cards from stored predictions and quotes
(rest notes, players out with play probability), grading of paper bets, and that the shared
page renders NFL cards exactly as before."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from betdata.core.matching import sport_id
from betdata.db.schema import books, games, markets, model_predictions, odds_snapshots, results
from betdata.db.schema import teams as teams_t
from betdata.nba import live
from betdata.page.build import card_html
from betdata.sports import Card

NOW = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)  # 2 PM EDT


@pytest.fixture
def design(monkeypatch):
    d = live.Design(groups=["avail"], lam_m=10, lam_t=10, spread_threshold=3.0,
                    ml_threshold=4.0, blend_weight=0.3, fit="config/fits/none.json")
    monkeypatch.setattr(live.Design, "load", classmethod(lambda cls, path=None: d))
    return d


def nba():
    from betdata.sports import load_entries
    from betdata.sports.nba import NbaSport

    return NbaSport(next(e for e in load_entries() if e.key == "nba"))


def _game(conn, tip, status="scheduled"):
    sid = sport_id(conn, "nba")
    ids = dict(conn.execute(select(teams_t.c.abbrev, teams_t.c.team_id)
                            .where(teams_t.c.sport_id == sid)).all())  # fmt: skip
    return conn.execute(insert(games).values(
        sport_id=sid, season=2026, season_type="REG", start_time_utc=tip, home_team_id=ids["BOS"],
        away_team_id=ids["NYK"], status=status)).inserted_primary_key[0]  # fmt: skip


def _quote(conn, gid, line, ml_home, ml_away, at):
    book = conn.execute(select(books.c.book_id).where(books.c.key == "draftkings")).scalar()
    mk = dict(conn.execute(select(markets.c.key, markets.c.market_id)).all())
    for market, outcome, ln, price in (("spreads", "home", line, -110),
                                       ("spreads", "away", -line, -110),
                                       ("h2h", "home", None, ml_home),
                                       ("h2h", "away", None, ml_away)):  # fmt: skip
        conn.execute(insert(odds_snapshots).values(
            source="espn_nba", source_game_id=str(gid), game_id=gid, book_id=book,
            market_id=mk[market], outcome=outcome, line=ln, price_american=price,
            snapshot_kind="poll", fetched_at=at))  # fmt: skip


def _pred(conn, gid, at, margin, wp, point="morning", spread=None, mlh=None, mla=None):
    return conn.execute(insert(model_predictions).values(
        model="nba", model_version="nba:test", game_id=gid, predicted_at=at,
        pred_home_margin=margin, pred_home_win_prob=wp, pred_total=221.0,
        prediction_point=point, market_spread_home=spread, market_ml_home=mlh,
        market_ml_away=mla,
        factors={"margin": [["players newly out", 2.4], ["rest days", 1.1], ["travel", 0.4],
                            ["altitude", 0.1]],
                 "missing": {"home": [], "away": ["Jalen Brunson (Questionable, 55% to play)"]},
                 "notes": ["NYK: 2nd night of a back-to-back"]})).inserted_primary_key[0]
    # fmt: skip


def test_cards(engine, design):
    tip = NOW + timedelta(hours=5)
    with engine.begin() as conn:
        gid = _game(conn, tip)
        _quote(conn, gid, -2.5, -140, 120, NOW - timedelta(hours=1))
        _pred(conn, gid, NOW - timedelta(minutes=5), 7.0, 0.72)
        [c] = nba().cards(conn, NOW)
    assert (c.home, c.away, c.point) == ("BOS", "NYK", "morning (game-day injury report)")
    assert c.model_spread_home == -7.0 and c.market_spread.line == -2.5
    assert c.spread_edge == 4.5 and c.spread_pick == "BOS"
    assert c.ml_edge == pytest.approx(15.8, abs=0.1) and c.ml_pick == "BOS"
    assert c.market_spread.book == "draftkings" and c.market_spread.source == "espn_nba"
    assert [f[0] for f in c.factors] == ["players newly out", "rest days", "travel"]
    assert c.missing["NYK"] == ["Jalen Brunson (Questionable, 55% to play)"]
    assert c.notes == ["NYK: 2nd night of a back-to-back"]
    html = card_html(c, {}, [], ml_flags=False)
    assert "2nd night of a back-to-back" in html and "55% to play" in html
    assert "Moneyline: BOS" not in html  # Part A: info only
    # The agreement rule: one DK quote, no Pinnacle -> a threshold pick, but not flagged.
    assert not c.spread_agree and "Spread: BOS" not in html and "not flagged" in html
    # An earlier DK line of BOS -2 means the line moved toward BOS: now it is flagged.
    with engine.begin() as conn:
        _quote(conn, gid, -2.0, -130, 110, NOW - timedelta(days=1))
        [c] = nba().cards(conn, NOW)
    html = card_html(c, {}, [], ml_flags=False)
    assert c.spread_agree.code == "move" and "Spread: BOS -2.5" in html
    assert "DK line moved toward BOS: BOS -2 → -2.5" in html


def test_no_market_line(engine, design):
    with engine.begin() as conn:
        gid = _game(conn, NOW + timedelta(hours=5))
        _pred(conn, gid, NOW - timedelta(minutes=5), 3.0, 0.6)
        [c] = nba().cards(conn, NOW)
    assert c.market_spread is None and c.spread_edge is None
    assert "No market lines yet." in card_html(c, {}, [])


def test_grading(engine, design):
    tip = NOW - timedelta(hours=4)
    with engine.begin() as conn:
        gid = _game(conn, tip, status="final")
        conn.execute(insert(results).values(game_id=gid, home_score=110, away_score=100,
                                            source="espn_nba", final=True))  # fmt: skip
        _pred(conn, gid, tip - timedelta(hours=2), 8.0, 0.75, "pretip", spread=-3.0,
              mlh=-150, mla=130)  # fmt: skip
        out = nba().grade(conn, NOW)
        recs = {r.market: r for r in nba().records(conn)}
    assert out["inserted"] == 2
    assert (recs["spread"].wins, recs["spread"].losses) == (1, 0)  # 10 > 3
    assert recs["moneyline"].wins == 1 and recs["moneyline"].clv_unit == "% pts"


def test_nfl_card_unchanged_by_notes():
    c = Card(1, NOW, "KC", "BUF", "prekick", NOW, model_spread_home=-3.0)
    assert card_html(c, {}, []).count('<p class="meta">') == 1  # only the prediction point
