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
    assert '<span class="flag">Over 25.5</span>' in flagged
    assert "No player prop lines" in props_html([], {})


def test_version_label_and_unchanged_card():
    c = Card(1, NOW, "KC", "BUF", "prekick", NOW, model_spread_home=-3.0)
    plain = card_html(c, {}, [])
    labelled = card_html(c, {}, [], True, "Model version: m13w · newer version m15 running in "
                         "the background (graded live, not shown)")  # fmt: skip
    assert labelled.replace('\n  <p class="meta ver">Model version: m13w · newer version m15 '
                            'running in the background (graded live, not shown)</p>', "") == plain
    assert version_note(DummySport.__new__(DummySport)) is None  # no version: no label
