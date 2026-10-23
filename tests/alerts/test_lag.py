"""M16 lag study (Part E): lag per book from publication to the first move, censoring at 60,
the baseline from before publication, and the once-only Signal 1 decision."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import insert

from betdata.alerts import lag as L
from betdata.alerts.db import engine_for, get_state, quotes, status_events

T0 = datetime(2026, 11, 2, 18, 30, tzinfo=UTC)  # the report's stated time


def _event(p, gid=7, at=T0):
    return p.execute(insert(status_events).values(
        sport="nba", game_id=gid, player_key="1", player_name="Star", old_status="Questionable",
        new_status="Out", prob_before=0.55, prob_after=0.0, qualifies=True, report_at=at,
        detected_at=at + timedelta(minutes=2))).inserted_primary_key[0]  # fmt: skip


def _poll(p, t, book, home_line, gid=7, h2h=(-150, 130)):
    rows = [("spreads", "home", home_line, -110), ("spreads", "away", -home_line, -110),
            ("h2h", "home", None, h2h[0]), ("h2h", "away", None, h2h[1])]
    p.execute(insert(quotes), [dict(polled_at=t, sport="nba", game_id=gid, source="sharpapi",
                                    book=book, market=m, outcome=o, line=line, price=pr)
                               for m, o, line, pr in rows])  # fmt: skip


def test_lags_censoring_and_decision(tmp_path):
    eng = engine_for(tmp_path / "p.sqlite")
    with eng.begin() as p:
        _event(p)
        _poll(p, T0 - timedelta(minutes=20), "draftkings", -4.5)  # baselines
        _poll(p, T0 - timedelta(minutes=20), "fanduel", -4.5)
        _poll(p, T0 + timedelta(minutes=3), "draftkings", -4.5)
        _poll(p, T0 + timedelta(minutes=3), "fanduel", -2.5)  # FD moved 2 pts by the first poll
        _poll(p, T0 + timedelta(minutes=9), "draftkings", -3.5)  # DK moved 1 pt at +9
        lags = {x.book: x for x in L.event_lags(p, None, p.execute(
            status_events.select()).one())}  # fmt: skip
    assert (lags["fanduel"].minutes, lags["fanduel"].market) == (3.0, "spreads")
    assert lags["draftkings"].minutes == 9.0 and not lags["draftkings"].censored
    assert lags["pinnacle"].minutes is None  # no Pinnacle poll in this window

    with eng.begin() as p:  # an event where nothing moves: censored at 60
        _event(p, gid=8, at=T0 + timedelta(hours=2))
        _poll(p, T0 + timedelta(hours=1), "draftkings", -1.5, gid=8)
        _poll(p, T0 + timedelta(hours=2, minutes=5), "draftkings", -1.5, gid=8,
              h2h=(-152, 132))  # under 1.5 pp of no-vig
        lags = L.all_lags(p, None, T0 + timedelta(hours=4))
    cens = [x for x in lags if x.game_id == 8 and x.book == "draftkings"][0]
    assert cens.minutes == 60.0 and cens.censored
    s = L.summary(lags)
    assert s["soft_n"] == 3 and s["soft_median"] == 9.0

    with eng.begin() as p:  # no decision before two weeks; then exactly once
        assert L.decide(p, None, T0 + timedelta(days=13)) is None
        d = L.decide(p, None, T0 + timedelta(days=15))
        assert d["signal1"] == "on" and d["soft_median_min"] == 9.0
        assert get_state(p, "signal1:off") is None
        assert L.decide(p, None, T0 + timedelta(days=30)) == d


def test_fast_books_turn_signal1_off(tmp_path):
    eng = engine_for(tmp_path / "p.sqlite")
    with eng.begin() as p:
        _event(p)
        for b in ("draftkings", "fanduel"):
            _poll(p, T0 - timedelta(minutes=30), b, -4.5)
            _poll(p, T0 + timedelta(minutes=2), b, -2.0)
        d = L.decide(p, None, T0 + timedelta(days=15))
        assert d["signal1"].startswith("off") and get_state(p, "signal1:off") is True
