"""Pick labels and stakes (alerts/labels.py, MODEL.md "Pick labels and stakes"): the fixed
TAKE / LEAN / PASS rules at creation, and the Live / Moved / Gone display status."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert

from betdata.alerts import labels as LB
from betdata.alerts.db import engine_for, quotes
from betdata.alerts.notify import Alert

NOW = datetime(2026, 11, 1, 20, 0, tzinfo=UTC)
TIP = NOW + timedelta(hours=3)


@pytest.fixture
def pdb(tmp_path):
    return engine_for(tmp_path / "poller.sqlite")


def _quote(p, book, away, home, line=4.5, at=NOW - timedelta(minutes=2), market="spreads"):
    """Both sides of a spread (the away side at +line), or of a total (over/under at line)."""
    sides = (("away", line, away), ("home", -line, home)) if market == "spreads" else (
        ("over", line, away), ("under", line, home))
    for outcome, ln, price in sides:
        p.execute(insert(quotes).values(polled_at=at, sport="nba", game_id=1, source="sharpapi",
                                        book=book, market=market, outcome=outcome, line=ln,
                                        price=price))  # fmt: skip


def _value(edge, src, two_book=False, signal="value", **kw):
    d = {"edge": edge, "edge_source": src, "two_book_or_model_only": two_book,
         "consensus_before": {"two_book": two_book}}
    return Alert(signal, "nba", 1, "NYK @ BOS", TIP, "spreads", "away", 4.5, -110,
                 "draftkings", "r", "k", 0.5 + edge, 0.5, None, None, False,
                 {**d, **kw})  # fmt: skip


def test_take_needs_one_and_a_half_thresholds_and_news_or_pinnacle(pdb):
    with pdb.begin() as p:
        _quote(p, "draftkings", -110, -110)
    with pdb.connect() as p:
        take = LB.label(p, _value(0.045, "news"), NOW)  # exactly 1.5 x 3 pp
        lean = LB.label(p, _value(0.044, "news"), NOW)
        book = LB.label(p, _value(0.06, "book disagreement"), NOW)  # no Pinnacle
    assert (take["label"], take["stake"], take["threshold"]) == ("TAKE", 1.0, 0.03)
    assert (lean["label"], lean["stake"]) == ("LEAN", 0.5)
    assert book["label"] == "LEAN" and book["pinnacle_sides"] is None and book["available"]


def test_a_fresh_pinnacle_price_on_the_pick_makes_it_take(pdb):
    with pdb.begin() as p:
        _quote(p, "draftkings", -110, -110)
        _quote(p, "pinnacle", -130, 110)  # away no-vig 54.3% > -110's break-even 52.4%
    with pdb.connect() as p:
        lab = LB.label(p, _value(0.06, "book disagreement"), NOW)
        # book disagreement against a two-book consensus is LEAN at most (two-book prices
        # need 1.5x: 4.5 pp, so 9 pp is 1.5x of that)
        two = LB.label(p, _value(0.10, "book disagreement", two_book=True), NOW)
        late = LB.label(p, _value(0.06, "book disagreement"), NOW + timedelta(minutes=11))
    assert lab["label"] == "TAKE" and lab["pinnacle_sides"] is True
    assert two["label"] == "LEAN" and two["threshold"] == 0.045
    assert late["label"] == "LEAN" and late["pinnacle_sides"] is None  # 13 minutes old


def test_pinnacle_against_the_pick_is_not_take(pdb):
    with pdb.begin() as p:
        _quote(p, "draftkings", -110, -110)
        _quote(p, "pinnacle", 105, -125)
    with pdb.connect() as p:
        lab = LB.label(p, _value(0.06, "book disagreement"), NOW)
    assert lab["label"] == "LEAN" and lab["pinnacle_sides"] is False


def test_a_price_gone_at_the_latest_poll_is_pass(pdb):
    with pdb.begin() as p:
        _quote(p, "draftkings", -110, -110, at=NOW - timedelta(minutes=9))
        _quote(p, "draftkings", -125, 105, at=NOW - timedelta(minutes=1))
    with pdb.connect() as p:
        lab = LB.label(p, _value(0.08, "news"), NOW)
    assert (lab["label"], lab["stake"], lab["available"]) == ("PASS", 0.0, False)


def test_other_signals_and_thresholds():
    snipe = Alert("snipe", "nba", 1, "NYK @ BOS", TIP, "rebounds", "over", 6.5, 110,
                  "draftkings", "r", "s", 0.6, 0.5, "A Player", None, False, {"edge": 0.09})
    g1 = Alert("g1", "nba", 1, "NYK @ BOS", TIP, "totals", "under", 220.0, -110,
               "fanduel", "r", "g", None, 0.5, None, None, True, {"open": 221.0})
    g2 = Alert("g2", "nfl", 1, "A @ B", TIP, "spreads", "home", -2.5, 120, "fanduel", "r",
               "g2", 0.5, 0.44, None, None, True, {})
    assert LB.edge_and_threshold(snipe) == (0.09, 0.06)
    assert LB.edge_and_threshold(g1) == (1.0, 0.5)  # points below the opening total
    assert LB.edge_and_threshold(g2) == (pytest.approx(0.06), 0.04)
    news = Alert("news", "nba", 1, "NYK @ BOS", TIP, "spreads", "away", 4.5, -110,
                 "draftkings", "r", "n")
    assert LB.label(None, news, NOW) is None  # M16 signals are not labelled


def test_display_status(pdb):
    a = _value(0.06, "news")  # fair 56%
    with pdb.begin() as p:
        _quote(p, "draftkings", -110, -110, at=NOW - timedelta(minutes=30))
    with pdb.connect() as p:
        assert LB.status(p, a, NOW) == "Live"
    with pdb.begin() as p:
        _quote(p, "draftkings", -120, 100, at=NOW - timedelta(minutes=20))  # 54.5% < 56%
    with pdb.connect() as p:
        assert LB.status(p, a, NOW) == "Moved"
    with pdb.begin() as p:
        _quote(p, "draftkings", -140, 120, at=NOW - timedelta(minutes=10))  # 58.3% >= 56%
    with pdb.connect() as p:
        assert LB.status(p, a, NOW) == "Gone"
    with pdb.begin() as p:
        _quote(p, "draftkings", -110, -110, line=3.5, at=NOW - timedelta(minutes=5))
    with pdb.connect() as p:
        assert LB.status(p, a, NOW) == "Gone"  # the line moved against the pick
    with pdb.begin() as p:
        _quote(p, "draftkings", -105, -115, line=5.5, at=NOW - timedelta(minutes=1))
    with pdb.connect() as p:
        assert LB.status(p, a, NOW) == "Live"  # a better line at a better price
