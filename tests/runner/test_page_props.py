"""M15 page: the props section (projection, line, P(over), edge, top 2 factors, source and
fetch time; research-only label; flags only where earned) and the side cards' version label.
A card renders exactly as before when no version note is given (verification a)."""

from datetime import UTC, datetime

from betdata.page.build import card_html, props_html, version_note
from betdata.sports import Card, PropRow
from tests.runner.dummy_sport import DummySport

NOW = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)


def _row(**kw):
    base = dict(game_id=1, kickoff=NOW, home="BOS", away="NYK", player="Jalen Brunson",
                market="points", mean=27.4, line=25.5, over_price=-115, under_price=-105,
                p_over=0.58, edge=4.6, factors=[["Robinson out", 1.2, "pts"],
                                                ["opponent defence", -0.6, "pts"]],
                book="draftkings", source="espn_props", fetched_at=NOW, point="morning",
                research=False, flag=None)  # fmt: skip
    return PropRow(**{**base, **kw})


def test_props_section():
    html = props_html([_row(), _row(player="Josh Hart", market="pra", research=True,
                                    edge=9.0)], {"draftkings": "DraftKings"})  # fmt: skip
    assert "NYK @ BOS" in html and "Proj <b>27.4</b>" in html and "line 25.5" in html
    assert "o -115 / u -105" in html and "P(over) 58%" in html and "edge +4.6 pp" in html
    assert "Robinson out +1.2 pts; opponent defence -0.6 pts" in html
    assert "DraftKings via ESPN" in html
    assert html.count("research only, not backtested") == 1  # PRA only
    assert "class=\"flag\"" not in html  # no flag unless the market earned it
    flagged = props_html([_row(flag="over")], {})
    assert 'class="flag"' not in flagged and "model leans over, research only" in flagged
    assert "No player prop lines" in props_html([], {})
    tiny = props_html([_row(factors=[["game script", 0.01, "rec"], ["wind", -3.0, "yds"]])], {})
    assert "game script" not in tiny and "wind -3.0 yds" in tiny


def test_version_label_and_unchanged_card():
    c = Card(1, NOW, "KC", "BUF", "prekick", NOW, model_spread_home=-3.0)
    plain = card_html(c, {}, [])
    note = "Model version: m13w · also running in the background (graded live, not shown): m15"
    labelled = card_html(c, {}, [], True, note)
    assert labelled.replace(f'\n  <p class="meta ver">{note}</p>', "") == plain
    assert version_note(DummySport.__new__(DummySport)) is None  # no version: no label


def test_info_only_label_and_flag_rule():
    from betdata.nba.props.live import PropDesign

    d = PropDesign(fit="x", thresholds={"threes": 8.0, "assists": 10.0, "points": 6.0},
                   test_roi={"threes": 0.0091, "assists": 0.0504, "points": -0.1},
                   test_roi_95={"threes": [-0.0173, 0.0356], "assists": [0.0106, 0.0902],
                                "points": [-0.15, -0.05]})  # fmt: skip
    assert d.info_only("threes") and not d.flag_allowed("threes")
    assert d.flag_allowed("assists") and not d.info_only("assists")
    assert not d.flag_allowed("points") and not d.info_only("points")
    html = props_html([_row(market="threes", info=True)], {})
    assert "info only (backtest ROI range includes 0)" in html
    assert "research only" not in html


def test_live_config_keeps_threes_info_only():
    from betdata.nba.props.live import PropDesign

    d = PropDesign.load()
    assert d.info_only("threes") and not d.flag_allowed("threes")
    assert d.flag_allowed("rebounds") and d.flag_allowed("assists")
