
"""A stub second sport for the Part E test: no data, no model, one fixed game."""

from datetime import timedelta

from sqlalchemy import insert, select

from betdata.agreement import Agreement
from betdata.core.matching import sport_id
from betdata.db.schema import games, teams
from betdata.sports import Card, RecordRow, Sport

CALLS: list[str] = []


class DummySport(Sport):
    ML_ROI: float | None = None  # final-test moneyline ROI the page sees
    # the spread agreement rule: the card flags its spread pick only with an agreeing signal
    AGREE = Agreement(("move",), ("DK line moved toward HOMEX: HOMEX -1.5 → -2.5",))

    def predict(self, engine, now):
        CALLS.append("predict")
        return 1

    def cards(self, conn, now):
        return [Card(10_001, now + timedelta(hours=5), "HOMEX", "AWAYX", "stub", now,
                     model_spread_home=-2.5, spread_edge=4.0, spread_pick="HOMEX",
                     spread_agree=self.AGREE,
                     model_win_home=0.6, ml_edge=5.0, ml_pick="HOMEX",
                     factors=[("stub factor", 1.5)],
                     missing={"HOMEX": ["Player Zed"]})]  # fmt: skip

    def grade(self, conn, now):
        CALLS.append("grade")
        return {"inserted": 1, "updated": 0, "pending": 0}

    def records(self, conn):
        return [RecordRow("spread", "dummy:v1", 7, 3, 0, 0, 3.6, 0.8, 0.6, "pts")]

    def thresholds(self):
        return {"spread": "stub threshold", "moneyline": "stub ml threshold"}

    def moneyline_final_roi(self):
        return self.ML_ROI

    def bootstrap(self, engine, now, source_kwargs=None):
        """Pretend history: enough final games of this sport for the runner."""
        CALLS.append("bootstrap")
        with engine.begin() as conn:
            sid = sport_id(conn, self.key)
            a, b = conn.execute(select(teams.c.team_id).limit(2)).scalars().all()
            conn.execute(insert(games), [
                dict(sport_id=sid, season=2026, start_time_utc=now - timedelta(days=i + 1),
                     home_team_id=a, away_team_id=b, status="final")
                for i in range(500)])  # fmt: skip
