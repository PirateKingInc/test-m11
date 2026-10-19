"""REVIEW.md #2: M11's injury status counts only rows published by the final report."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select

from betdata.analysis.context import load_context
from betdata.core.matching import sport_id
from betdata.db.schema import games, injuries, teams


def test_rows_published_after_the_final_report_are_ignored(engine):
    kickoff = datetime(2024, 10, 6, 17, 0, tzinfo=UTC)  # Sunday 1 pm ET; report Friday 5 pm
    backfilled = datetime(2026, 10, 1, tzinfo=UTC)
    with engine.begin() as conn:
        sid = sport_id(conn, "nfl")
        home, away = conn.execute(
            select(teams.c.team_id).where(teams.c.sport_id == sid).limit(2)
        ).scalars().all()
        conn.execute(insert(games).values(sport_id=sid, season=2024, week=5,
                     start_time_utc=kickoff, home_team_id=home, away_team_id=away))  # fmt: skip
        common = dict(sport_id=sid, team_id=home, season=2024, week=5, source="nflverse",
                      fetched_at=backfilled)  # fmt: skip
        conn.execute(insert(injuries), [
            dict(common, player_name="On Time", source_player_id="A", report_status="Out",
                 report_date=kickoff - timedelta(days=2, hours=1)),
            dict(common, player_name="Too Late", source_player_id="B", report_status="Out",
                 report_date=kickoff - timedelta(hours=3)),
        ])  # fmt: skip
        ctx = load_context(conn)
    assert ctx.reports[(2024, 5, home)] == {"A": "Out"}
