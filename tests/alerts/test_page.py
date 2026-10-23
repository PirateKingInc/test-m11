"""The page's Alerts section (issue #168): rendering, insertion, and never breaking the page."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import insert

from betdata.alerts import page as P
from betdata.alerts.db import alert_grades, alerts, engine_for, set_state, status_events
from betdata.page.build import render

NOW = datetime(2026, 10, 26, 3, 30, tzinfo=UTC)


def _site(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    status = {"last_ok_at": NOW.isoformat(), "stale_hours": 3, "missing_keys": []}
    html = render([], {}, status)  # the hourly page as the existing code builds it
    (site / "index.html").write_text(html)
    return site, html


def _db(tmp_path):
    db = tmp_path / "poller.sqlite"
    pdb = engine_for(db)
    with pdb.begin() as p:
        base = dict(sport="nba", game_id=1, game_label="NYK @ BOS", tip=NOW - timedelta(hours=4),
                    book="draftkings", reason="Pinnacle no-vig 55.0% vs draftkings 48.0%",
                    created_at=NOW - timedelta(hours=6))  # fmt: skip
        a1 = p.execute(insert(alerts).values(
            signal="sharp", market="spreads", side="home", line=-3.5, price=-110,
            dedup_key="a1", sent=True, quiet=False, **base)).inserted_primary_key[0]  # fmt: skip
        p.execute(insert(alerts).values(
            signal="news", market="points", side="over", line=25.5, price=-115,
            player_name="Jayson Tatum", dedup_key="a2", sent=False, quiet=True, study=True,
            **base))  # fmt: skip
        p.execute(insert(alert_grades).values(alert_id=a1, clv_points=1.5, clv_novig=0.031,
                                              result="win", profit=0.9091, graded_at=NOW))
        p.execute(insert(status_events).values(
            sport="nba", game_id=1, player_key="p", player_name="X", qualifies=True,
            detected_at=NOW - timedelta(hours=8), new_status="Out"))  # fmt: skip
    return db, pdb


def test_section_rendered_and_inserted_before_main_end(tmp_path):
    site, original = _site(tmp_path)
    db, _ = _db(tmp_path)
    assert P.publish(db, site, NOW) == "alerts section added"
    html = (site / "index.html").read_text()
    assert html.count('id="alerts"') == 1 and "Paper alerts, not advice." in html
    assert "No signal is judged before 200 graded alerts" in html
    assert "BOS -3.5" in html and "Jayson Tatum over 25.5 points" in html
    assert "win CLV +3.1 pp (+1.5 pts)" in html and "quiet" in html and "pending" in html
    assert "Lag study: 1 qualifying changes so far; DK+FD pooled median lag not enough data; " \
           "Signal 1 on (study running)." in html
    # Only the fragment was added, just before </main>.
    frag_start, frag_end = html.index(P.START), html.index(P.END) + len(P.END)
    assert html[:frag_start] + html[frag_end:] == original
    assert html[frag_end:].startswith("</main>")
    data = json.loads((site / "alerts.json").read_text())
    assert data["available"] and data["signals"]["sharp"]["n_novig"] == 1
    assert [a["signal"] for a in data["alerts"]] == ["news", "sharp"]
    # A second run replaces the section; it does not add another.
    P.publish(db, site, NOW)
    assert (site / "index.html").read_text().count(P.START) == 1


def test_lag_line_shows_the_decision(tmp_path):
    db, pdb = _db(tmp_path)
    with pdb.begin() as p:
        set_state(p, "signal1:decision", {"signal1": "off: not viable on free data"}, NOW)
    frag = P.fragment(P.load(db, NOW))
    assert "Signal 1 off: not viable on free data." in frag


def test_missing_or_broken_db_never_breaks_the_page(tmp_path):
    site, original = _site(tmp_path)
    assert P.publish(tmp_path / "nope.sqlite", site, NOW) == "alerts section unavailable"
    html = (site / "index.html").read_text()
    assert "Alerts unavailable." in html and "Paper alerts, not advice." in html
    assert not (tmp_path / "nope.sqlite").exists()  # read-only: never created
    assert json.loads((site / "alerts.json").read_text())["available"] is False
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not a database at all" * 50)
    assert P.publish(bad, site, NOW) == "alerts section unavailable"
    html = (site / "index.html").read_text()
    s, e = html.index(P.START), html.index(P.END) + len(P.END)
    assert html[:s] + html[e:] == original
    # No built page: nothing to do, nothing written.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert P.publish(bad, empty, NOW).startswith("no site/index.html")
    assert list(empty.iterdir()) == []


def test_insert_fallbacks():
    frag = f"{P.START}x{P.END}"
    assert P.insert("<body><p>a</p></body>", frag) == f"<body><p>a</p>{frag}</body>"
    assert P.insert("plain", frag) == "plain"
    once = P.insert("<main>m</main>", frag)
    assert P.insert(once, f"{P.START}y{P.END}") == f"<main>m{P.START}y{P.END}</main>"
