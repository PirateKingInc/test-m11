"""M17: ESPN's NFL prop lines carry no player name; the engine fills it from the rosters."""

from sqlalchemy import insert, select

from betdata.db.schema import roster_weeks, teams
from betdata.nfl.engine.live import roster_names


def test_roster_names_maps_gsis_to_the_latest_name(engine):
    with engine.begin() as conn:
        team = conn.execute(select(teams.c.team_id).limit(1)).scalar()
        rows = [(2025, 1, "00-0000001", "Old Spelling"), (2026, 3, "00-0000001", "New Spelling"),
                (2026, 3, "00-0000002", "Other Player"), (2024, 1, "00-0000003", "Too Old"),
                (2026, 3, None, "No Id")]
        for season, week, gsis, name in rows:
            conn.execute(insert(roster_weeks).values(
                source="test", season=season, week=week, team_id=team, gsis_id=gsis,
                player_name=name, status="ACT"))
        names = roster_names(conn, 2025)
    assert names == {"00-0000001": "New Spelling", "00-0000002": "Other Player"}


def test_page_shows_the_roster_name_for_rows_stored_with_the_espn_id():
    from betdata.nfl.engine.page import display_name

    names = {"00-0000001": "Jordan Love"}
    row = {"player_id": "4036378", "player_name": "4036378", "dist": {"gsis_id": "00-0000001"}}
    assert display_name(row, names)["player_name"] == "Jordan Love"
    named = {"player_id": "1", "player_name": "Kept", "dist": {"gsis_id": "00-0000001"}}
    assert display_name(named, names)["player_name"] == "Kept"
    unknown = {"player_id": "2", "player_name": "2", "dist": {"gsis_id": "00-9"}}
    assert display_name(unknown, names)["player_name"] == "2"
