"""M18 Page: each sport's tab opens with a Value section (live alerts, recent graded ones, and
per signal the running CLV vs the consensus close, the count toward 200 and the ROI); the
model sections follow, labelled research only."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import insert

from betdata.alerts.db import alert_grades, alerts, consensus_grades, engine_for
from betdata.page.build import et
from betdata.page.value import value_html

NOW = datetime(2026, 11, 1, 20, 0, tzinfo=UTC)


def _alert(p, key, tip, clv=None, result=None, sport="nba", signal="value"):
    aid = p.execute(insert(alerts).values(
        signal=signal, sport=sport, game_id=1, game_label="NYK @ BOS", tip=tip,
        market="spreads", side="away", line=4.5, price=-110, book="draftkings", fair_prob=0.58,
        book_prob=0.5, reason="Star Out (report 1:45 PM ET)", dedup_key=key,
        created_at=tip - timedelta(hours=2), quiet=False, sent=True)).inserted_primary_key[0]
    if clv is not None:
        p.execute(insert(consensus_grades).values(alert_id=aid, close_prob=0.5 + clv, clv=clv,
                                                  close_method="pinnacle", close_n=1,
                                                  close_two_book=False, graded_at=NOW))
    if result is not None:
        p.execute(insert(alert_grades).values(alert_id=aid, result=result,
                                              profit=0.909 if result == "win" else -1.0,
                                              graded_at=NOW))  # fmt: skip
    return aid


def test_value_section(tmp_path):
    pdb = engine_for(tmp_path / "poller.sqlite")
    with pdb.begin() as p:
        _alert(p, "live", NOW + timedelta(hours=3))
        _alert(p, "old1", NOW - timedelta(days=1), clv=0.03, result="win")
        _alert(p, "old2", NOW - timedelta(days=2), clv=-0.01, result="loss")
    with pdb.connect() as p:
        html = value_html("nba", p, NOW, et)
        nfl = value_html("nfl", p, NOW, et)
    assert html.startswith("<section class='value'><h2>Value</h2>")
    assert "NYK +4.5 -110</b> at DraftKings" in html and "C-NBA" in html
    assert html.count("<li class='prop'>") == 3  # one live, two graded
    assert "CLV +3.0 pp" in html and "3 alerts, 2 of 200 graded" in html
    assert "1-1" in html and "no read before 60" in html
    assert "F</b> NBA opening-line prop snipes" in html and "no alerts yet (0 of 200)" in html
    assert "No live value alerts" in nfl and "C-NFL" in nfl and "C-NBA" not in nfl


def test_no_poller_database():
    assert "Value alerts unavailable" in value_html("nba", None, NOW, et)


def test_a_poller_database_without_the_m18_tables(tmp_path):
    from sqlalchemy import create_engine, text

    eng = create_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    with eng.begin() as c:
        c.execute(text("create table alerts (alert_id integer primary key)"))
    with eng.connect() as c:
        html = value_html("nba", c, NOW, et)
    assert "No live value alerts" in html and "C-NBA" in html and "0 of 200" in html
