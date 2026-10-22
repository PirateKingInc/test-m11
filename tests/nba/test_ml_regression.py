"""M15.1 moneyline grading audit: real NBA games of 2026-10-21 as the live page graded them.

The live record read nba15 0-20 and M14 6-12. The audit (REVIEW M15.1-1) found the picks and
grades correct but (1) each game counted once per prediction point (up to three times), and
(2) the frozen calibration the final test used was left out live. These games pin both down:
the side, the price, the result and the profit, and one counted pick per game."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select

from betdata import moneyline as ml
from betdata.core.matching import sport_id
from betdata.db.schema import games, model_predictions, nba_bet_grades, results, teams
from betdata.nba.grade import grade_nba, records

CAL = ml.Calibration(0.9752506347268594, -0.021033754854447192, "validation", True)
# (home, away, tip UTC, home score, away score,
#  [(point, hours before tip, nba15 home win prob, home price, away price)])
GAMES = [
    ("TOR", "CHI", datetime(2026, 10, 21, 23, 30, tzinfo=UTC), 135, 92,
     [("early", 6.5, 0.5688, -440, 340), ("morning", 1.5, 0.577, -470, 360),
      ("pretip", 0.5, 0.5789, -470, 360)]),
    ("MIA", "MIN", datetime(2026, 10, 21, 23, 30, tzinfo=UTC), 106, 131,
     [("morning", 1.5, 0.5817, -122, 102), ("pretip", 0.5, 0.5818, -118, -102)]),
    ("BKN", "CHA", datetime(2026, 10, 21, 23, 30, tzinfo=UTC), 103, 110,
     [("pretip", 0.5, 0.3358, 100, -120)]),
    ("LAC", "SAC", datetime(2026, 10, 22, 2, 30, tzinfo=UTC), 121, 96,
     [("pretip", 0.5, 0.6946, -185, 154)]),
]  # fmt: skip


def _load(engine):
    with engine.begin() as conn:
        sid = sport_id(conn, "nba")
        ab = dict(conn.execute(select(teams.c.abbrev, teams.c.team_id)
                               .where(teams.c.sport_id == sid)).all())  # fmt: skip
        ids = []
        for home, away, tip, hs, aw, preds in GAMES:
            gid = conn.execute(insert(games).values(
                sport_id=sid, season=2026, start_time_utc=tip, home_team_id=ab[home],
                away_team_id=ab[away], status="final")).inserted_primary_key[0]  # fmt: skip
            conn.execute(insert(results).values(game_id=gid, source="espn_nba", home_score=hs,
                                                away_score=aw, final=True))  # fmt: skip
            for point, hrs, wp, mlh, mla in preds:
                conn.execute(insert(model_predictions).values(
                    model="nba15", model_version="nba15:test", game_id=gid,
                    predicted_at=tip - timedelta(hours=hrs), pred_home_margin=0.0,
                    pred_home_win_prob=wp, prediction_point=point, market_ml_home=mlh,
                    market_ml_away=mla))  # fmt: skip
            ids.append(gid)
    return ids


def test_real_games_grade_the_right_side_and_count_once(engine):
    tor, mia, bkn, lac = _load(engine)
    with engine.begin() as conn:
        grade_nba(conn, datetime(2026, 10, 23, tzinfo=UTC), 6.0, 5.0, CAL)
        g = conn.execute(select(nba_bet_grades.c.game_id, nba_bet_grades.c.side,
                                nba_bet_grades.c.price_american, nba_bet_grades.c.result,
                                nba_bet_grades.c.profit)
                         .where(nba_bet_grades.c.market == "moneyline")).all()  # fmt: skip
        rec = {r.market: r for r in records(conn)}
    by = {}
    for r in g:
        by.setdefault(r.game_id, set()).add((r.side, r.price_american, r.result, r.profit))
    # TOR -470 at home, model 57%: the model backs CHI +360; TOR won by 43, a loss.
    assert by[tor] == {("away", 340, "loss", -1.0), ("away", 360, "loss", -1.0)}
    # MIA vs MIN, pretip: calibrated 57.5% vs no-vig 51.7% clears 5 pts; MIA lost 106-131.
    # The morning pick (-122, no-vig 52.6%) clears 5 pts only uncalibrated (4.9 calibrated).
    assert by[mia] == {("home", -118, "loss", -1.0)}
    # BKN +100 / CHA -120, model 34% for BKN: the model backs CHA at -120 and wins.
    assert by[bkn] == {("away", -120, "win", 0.8333)}
    # LAC -185 (no-vig 62.2%), calibrated 68.6%: home, won 121-96.
    assert by[lac] == {("home", -185, "win", 0.5405)}
    # One counted pick per game: TOR's three prediction points count once (the pretip one).
    ml_rec = rec["moneyline"]
    assert (ml_rec.wins, ml_rec.losses, ml_rec.bets) == (2, 2, 4)


def test_regrade_removes_a_pick_that_no_longer_qualifies(engine):
    _, mia, *_ = _load(engine)
    now = datetime(2026, 10, 23, tzinfo=UTC)
    with engine.begin() as conn:
        q = select(nba_bet_grades.c.price_american).where(nba_bet_grades.c.game_id == mia)
        grade_nba(conn, now, 6.0, 5.0)  # uncalibrated, as the live page did: both MIA points
        assert sorted(conn.execute(q).scalars()) == [-122, -118]
        grade_nba(conn, now, 6.0, 5.0, CAL)  # the corrected rule: the morning pick goes
        assert list(conn.execute(q).scalars()) == [-118]
