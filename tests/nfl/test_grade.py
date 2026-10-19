"""Step 3g: grading on fixtures: win, loss, push, positive and negative CLV, no closing line,
postponed game; idempotent re-runs and score corrections."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, insert, select, update

from betdata.db.schema import (
    game_source_ids,
    games,
    model_predictions,
    nfl_bet_grades,
    odds_snapshots,
    results,
)
from betdata.nfl.grade import Bet, bets_for, clv, grade_nfl, records, result_of
from betdata.sources.nflverse import NflverseSource
from tests.conftest import fixture_bytes

NOW = datetime(2026, 10, 20, tzinfo=UTC)


@pytest.fixture
def db(engine, settings, fake_api, clock):
    nfl = NflverseSource(engine, settings.source("nflverse"), transport=fake_api.transport,
                         sleep=lambda s: None, clock=clock)  # fmt: skip
    fake_api.add(body=fixture_bytes("nflverse", "games_sample.csv"))
    assert nfl.run("schedules", seasons="all").status == "ok"
    return engine


def gid(conn, nflverse_id):
    return conn.execute(select(game_source_ids.c.game_id).where(
        game_source_ids.c.source_game_id == nflverse_id)).scalar()  # fmt: skip


def predict(conn, game, margin, total, spread_home, mkt_total, version="m12:test"):
    kickoff = conn.execute(select(games.c.start_time_utc).where(games.c.game_id == game)).scalar()
    return conn.execute(insert(model_predictions).values(
        model="m12", model_version=version, game_id=game, predicted_at=kickoff - timedelta(hours=2),
        pred_home_margin=margin, pred_total=total, market_spread_home=spread_home,
        market_total=mkt_total, prediction_point="prekick")).inserted_primary_key[0]  # fmt: skip


def grades(conn):
    return {(r.prediction_id, r.market): r for r in conn.execute(select(nfl_bet_grades))}


def test_bet_rules():
    assert bets_for(0.0, None, -8.5, None) == [Bet("spread", "away", 8.5, 8.5)]
    assert bets_for(6.0, None, -8.5, None) == []  # 2.5 points: below the 3-point threshold
    assert bets_for(None, 52.0, None, 47.5) == [Bet("total", "over", 47.5, 4.5)]
    assert result_of(Bet("spread", "home", -6.0, 4), 27, 21) == "push"
    assert clv(Bet("spread", "away", 10.0, 4), 8.5) == 1.5
    assert clv(Bet("total", "over", 48.5, 4), 47.5) == -1.0
    assert clv(Bet("total", "under", 48.5, 4), 47.5) == 1.0
    assert clv(Bet("spread", "home", -6.0, 4), None) is None


def test_grading_cases(db):
    with db.begin() as conn:
        phi = gid(conn, "2025_01_DAL_PHI")  # PHI 24-20, closed PHI -8.5, total 47.5 (44 pts)
        lac = gid(conn, "2025_01_KC_LAC")  # LAC 27-21, closed LAC +3, total 47.5 (48 pts)
        atl = gid(conn, "1999_01_MIN_ATL")  # ATL 14-17
        chi = gid(conn, "2026_06_CHI_ATL")  # not played
        # win with positive CLV: took DAL +10 when the close was +8.5; loss on the over
        # at 48.5 when the close was 47.5 (negative CLV)
        p1 = predict(conn, phi, 0.0, 55.0, -10.0, 48.5)
        # push: LAC -6 with LAC winning by 6; the close was LAC +3 (CLV -9)
        p2 = predict(conn, lac, 10.0, None, -6.0, None)
        # no closing line: remove the 1999 game's closing odds
        conn.execute(delete(odds_snapshots).where(odds_snapshots.c.game_id == atl))
        p3 = predict(conn, atl, -10.0, None, -4.0, None)
        # postponed game: void
        conn.execute(update(games).where(games.c.game_id == chi).values(status="postponed"))
        p4 = predict(conn, chi, 10.0, None, -1.5, None)
        run = grade_nfl(conn, NOW)
        g = grades(conn)
    assert (run.inserted, run.updated) == (5, 0)
    assert (g[(p1, "spread")].result, g[(p1, "spread")].clv) == ("win", 1.5)
    assert g[(p1, "spread")].profit == pytest.approx(100 / 110, rel=1e-4)
    assert (g[(p1, "total")].result, g[(p1, "total")].clv) == ("loss", -1.0)
    assert (g[(p2, "spread")].result, g[(p2, "spread")].clv) == ("push", -9.0)
    assert g[(p3, "spread")].close_line is None and g[(p3, "spread")].clv is None
    assert g[(p3, "spread")].result == "win"  # took MIN +4; MIN won 17-14
    assert (g[(p4, "spread")].result, g[(p4, "spread")].profit) == ("void", 0.0)

    with db.begin() as conn:
        again = grade_nfl(conn, NOW + timedelta(days=1))
        assert (again.inserted, again.updated) == (0, 0)  # idempotent
        conn.execute(update(results).where(results.c.game_id == lac).values(home_score=28))
        fixed = grade_nfl(conn, NOW + timedelta(days=2))
        assert (fixed.inserted, fixed.updated) == (0, 1)  # a score correction regrades
        assert grades(conn)[(p2, "spread")].result == "win"
        recs = {(r.model_version, r.market): r for r in records(conn)}
    spread = recs[("m12:test", "spread")]
    assert (spread.wins, spread.losses, spread.pushes, spread.voids) == (3, 0, 0, 1)
    assert spread.avg_clv == pytest.approx((1.5 - 9.0) / 2)
    assert spread.beat_close == 0.5


def test_only_the_last_prediction_before_kickoff_counts(db):
    with db.begin() as conn:
        phi = gid(conn, "2025_01_DAL_PHI")
        kickoff = conn.execute(select(games.c.start_time_utc).where(games.c.game_id == phi)
                               ).scalar()  # fmt: skip
        early = predict(conn, phi, 0.0, None, -8.5, None)
        late = conn.execute(insert(model_predictions).values(
            model="m12", model_version="m12:test", game_id=phi, prediction_point="prekick",
            predicted_at=kickoff - timedelta(minutes=80), pred_home_margin=-9.0,
            market_spread_home=-8.5)).inserted_primary_key[0]  # fmt: skip
        conn.execute(insert(model_predictions).values(
            model="m12", model_version="m12:test", game_id=phi, prediction_point="prekick",
            predicted_at=kickoff + timedelta(minutes=5), pred_home_margin=20.0,
            market_spread_home=-8.5))  # after kickoff: never graded  # fmt: skip
        grade_nfl(conn, NOW)
        g = grades(conn)
    assert set(g) == set() or all(pid == late for pid, _ in g)
    assert early not in {pid for pid, _ in g}


def test_scheduler_job_grades(db, settings, clock):
    """`nfl_model.grade` is a local job the scheduler runs like any source (no network)."""
    from sqlalchemy import func

    from betdata.db.schema import ingest_runs
    from betdata.sources.nfl_model import NflModelSource

    with db.begin() as conn:
        predict(conn, gid(conn, "2025_01_DAL_PHI"), 0.0, None, -8.5, None)
    src = NflModelSource(db, settings.source("nfl_model"), clock=clock)
    result = src.run("grade")
    assert (result.status, result.rows_inserted) == ("ok", 1)
    assert src.run("grade").rows_inserted == 0  # idempotent
    with db.connect() as conn:
        assert conn.execute(select(func.count()).select_from(ingest_runs)).scalar() >= 3
