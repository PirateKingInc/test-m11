import pytest
from sqlalchemy import select

from betdata.core.matching import sport_id
from betdata.core.normalize.odds_math import american_to_decimal, decimal_to_american
from betdata.core.normalize.teams import TeamResolver, normalize_name
from betdata.db.schema import unmatched_entities


@pytest.mark.parametrize(
    "american,decimal", [(-110, 1.909091), (150, 2.5), (-450, 1.222222), (100, 2.0)]
)
def test_odds_conversion_round_trips(american, decimal):
    assert american_to_decimal(american) == decimal
    assert decimal_to_american(decimal) == american


def test_invalid_american_odds():
    with pytest.raises(ValueError):
        american_to_decimal(50)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("St. John's", "saint johns"),
        ("Ohio St.", "ohio state"),
        ("Texas A&M", "texas a and m"),
        ("Miami (OH)", "miami (oh)"),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_team_resolution_paths_and_unmatched_queue(engine):
    with engine.begin() as conn:
        r = TeamResolver(conn, sport_id(conn, "nfl"))
        chiefs = r.resolve("the_odds_api", "Kansas City Chiefs")
        assert chiefs is not None
        assert r.resolve("nflverse", "KC") == chiefs  # abbrev alias
        assert r.resolve("x", "kansas city chiefs") == chiefs  # normalized
        assert r.resolve("x", "Oakland Raiders") == r.resolve("x", "Las Vegas Raiders")
        assert r.resolve("x", "Kansas Cty Chiefs") is None  # typo: suggested, never auto-linked
        assert r.resolve("x", "Kansas Cty Chiefs") is None
        row = conn.execute(select(unmatched_entities)).mappings().one()
    assert row["times_seen"] == 2 and row["suggested_team_id"] == chiefs
