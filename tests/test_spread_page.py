"""The Spread view (owner decision 2026-11-10): the record, Results by day with the
"reconstructed" marker, and Tonight's card data (Spread picks only in the Spread view)."""

from datetime import date, timedelta

from betdata import official as O
from betdata import spread as SP
from betdata.alerts import tracker as T
from betdata.page.build import et
from betdata.page.official import days_html, header_html
from tests.test_spread import DAY, MADE, NOW, _alert, _ng, _pick, _quote, pdb  # noqa: F401
from tests.test_spread_records import _ledger


def test_spread_record_and_days_with_reconstructed_marker(tmp_path):
    led, site = tmp_path / "led", tmp_path / "site"
    led.mkdir()
    _ledger(led)
    rows = O.dataset(led)
    today = date(2026, 11, 10)
    h = header_html(rows, "nba", "NBA", today, site)
    sec = h[h.index("data-card='spread'"):]
    assert "Official record · NBA · Spread Picks" in sec and "1-0-0" in sec
    assert "Review at 50 graded picks: 1 of 50" in sec and "class='goal'" not in sec
    assert ">Spread</button>" in h  # the toggle's third option
    d = days_html(rows, "nba", today, site, et)
    sd = d[d.index("data-card='spread'"):]
    assert "<b>reconstructed</b>" in sd and "SPREAD 1u" in sd
    ng = d[:d.index("data-card='og'")]
    assert "reconstructed" not in ng and "dog-spread" not in ng  # not in Next Gen's days


def test_tracker_marks_spread_picks_with_their_trigger(pdb):  # noqa: F811
    with pdb.begin() as p:
        _alert(p, "dog")
        _quote(p, "draftkings", "home", 8.5, -110, MADE - timedelta(minutes=5))
        _ng(p, [_pick("dog")])
    SP.run(pdb, None, NOW)
    with pdb.connect() as p:
        doc = T.build(p, None, "nba", DAY, NOW)
    (g,) = doc["games"]
    sp = [x for x in g["picks"] if x.get("spread")]
    ml = [x for x in g["picks"] if x["card"]]
    assert len(sp) == 1 and len(ml) == 1
    assert sp[0]["label"] == "SPREAD" and sp[0]["stake"] == 1.0 and not sp[0]["card"]
    assert sp[0]["trigger"] == "from Next Gen ML CHA +284" and sp[0]["pick"] == "CHA +8.5"
    assert doc["spread_chosen"] is True and "spread_note" not in doc


def test_tracker_note_when_no_spread_picks(pdb):  # noqa: F811
    with pdb.begin() as p:
        _ng(p, [])
    SP.run(pdb, None, NOW)
    with pdb.connect() as p:
        doc = T.build(p, None, "nba", DAY, NOW)
    assert doc["spread_note"] == "No spread picks today — no Next Gen ML underdog on the card"
