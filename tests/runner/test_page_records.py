"""M15.1 season record layout and footer keys: the page version's spread and moneyline rows
only, one stated counting rule, "too few to judge" under 100 picks, ties at the close shown,
background versions collapsed; the footer names detected and missing keys."""

from betdata.page.build import detected_keys, missing_keys, records_html
from betdata.sports import RecordRow


def _row(market, version, w, lost, beat=0.5, ties=0):
    return RecordRow(market, version, w, lost, 0, 0, 0.1 * (w - lost), 0.3, beat, "pts", ties)


def test_record_layout():
    rows = [_row("spread", "nba15:abc", 2, 4, ties=3), _row("moneyline", "nba15:abc", 0, 7),
            _row("spread", "nba:old", 60, 50), _row("moneyline", "nba:old", 2, 4),
            _row("total", "nba15:abc", 1, 1)]
    html = records_html(rows, "nba15:abc")
    main, other = html.split("<details", 1)
    assert "What is counted: One pick per game and market" in main
    assert main.count("<tr><td>") == 2  # spread and moneyline of the page version only
    assert "Spread</td><td>2-4-0" in main and "Moneyline</td><td>0-7-0" in main
    assert main.count("too few to judge</span>") == 2 and "3 at the close" in main
    assert "nba:old" not in main and "Other model versions (background comparisons)" in other
    assert "nba:old" in other and "Total" in other


def test_record_page_version_by_name_and_100_picks():
    rows = [_row("spread", "m13w:746dc8cd89", 60, 45)]
    html = records_html(rows, "m13w")
    assert "60-45-0" in html.split("<details")[0] and "too few to judge</span>" not in html
    assert "No graded picks for this model version yet" in records_html(
        [_row("spread", "m12:x", 1, 0)], "m13w")


def test_footer_keys(settings, monkeypatch):
    monkeypatch.setenv("SHARPAPI_KEY", "x")
    monkeypatch.setenv("ODDS_API_KEY", "y")
    monkeypatch.delenv("ODDSPAPI_KEY", raising=False)
    found, missing = " ".join(detected_keys(settings)), " ".join(missing_keys(settings))
    assert "(SHARPAPI_KEY)" in found and "(ODDS_API_KEY)" in found
    assert "(ODDSPAPI_KEY)" in missing and "SHARPAPI_KEY" not in missing
