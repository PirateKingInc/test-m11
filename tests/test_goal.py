"""The official success goal (owner decision 2026-11-03; MODEL.md "Official success goal"): the
card at .500 or better AND positive in units over >= 500 graded card picks."""

import re

from betdata import official as O
from betdata.page.official import header_html
from tests.runner.test_page_m19 import TODAY, _ledger


def _g(w, lo, p=0, units=0.0, dec=2.0):
    return O.Goal(w, lo, p, units, w + lo + p, O.american(dec), 1 / dec)


def test_status_words():
    assert O.Goal(0, 0, 0, 0.0, 0, None, None).status == "too early"
    assert _g(6, 5, units=1.07).status == "too early · on track"
    assert _g(6, 5, units=-0.2).status == "too early · off track"  # units guard
    assert _g(5, 6, units=0.5).status == "too early · off track"  # below .500
    assert _g(260, 240, units=3.0).status == "goal met"
    assert _g(250, 250, units=0.01).status == "goal met"  # exactly .500 counts
    assert _g(260, 240, units=-3.0).status == "goal not met"


def test_pct_ignores_pushes_and_break_even_from_average_odds():
    g = _g(6, 5, 2, units=1.0, dec=2.19)
    assert abs(g.pct - 6 / 11) < 1e-12 and g.graded == 13
    assert O.context_text(g) == "avg odds +119 · break-even 45.7%"
    assert O.goal_text(_g(6, 5, units=1.07)).startswith(
        "Goal: .500+ and positive units over 500 graded picks — 6-5 (.545), +1.07u · 11 of 500")


def test_header_goal_matches_the_official_record(tmp_path):
    led, site = tmp_path / "ledger", tmp_path / "site"
    _ledger(led)
    rows = O.dataset(led)
    html = header_html(rows, "nba", "NBA", TODAY, site)
    rec = O.record(O.official(rows, "nba"), O.official_stake)
    m = re.search(r"data-goal='NBA' data-graded='(\d+)' data-pct='([^']*)' "
                  r"data-units='([^']*)' data-status='([^']*)'", html)
    assert int(m.group(1)) == rec.graded and float(m.group(3)) == round(rec.units, 4)
    assert m.group(4) == ("too early · on track" if rec.wins >= rec.losses and rec.units > 0
                          else "too early · off track")
    assert "data-goal='All sports'" in html  # once, on the NBA tab
    assert "<progress max='500' value='3'" in html
    nfl = header_html(rows, "nfl", "NFL", TODAY, site)
    assert "All sports" not in nfl and "data-goal='NFL'" in nfl
