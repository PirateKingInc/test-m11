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


def quote(conn, game, book, market, outcome, line, price, at, source="sharpapi"):
    """One polled book quote (game orientation) stored at `at`."""
    from betdata.db.schema import books, markets

    bid = conn.execute(select(books.c.book_id).where(books.c.key == book)).scalar()
    mid = conn.execute(select(markets.c.market_id).where(
        markets.c.key == market, markets.c.period == "game")).scalar()  # fmt: skip
    conn.execute(insert(odds_snapshots).values(
        source=source, source_game_id=f"t{game}", game_id=game, book_id=bid, market_id=mid,
        outcome=outcome, line=line, price_american=price, fetched_at=at))  # fmt: skip


def kick(conn, game):
    return conn.execute(select(games.c.start_time_utc).where(games.c.game_id == game)).scalar()


def two_way(conn, game, book, market, home_line, home_price, away_price, at):
    """Both sides of a spread (home line), total (the line; home=over) or moneyline."""
    sides = ("over", "under") if market == "totals" else ("home", "away")
    other = home_line if market == "totals" else (None if home_line is None else -home_line)
    quote(conn, game, book, market, sides[0], home_line, home_price, at)
    quote(conn, game, book, market, sides[1], other, away_price, at)


@pytest.fixture
def poller(tmp_path, monkeypatch):
    """A poller database (Pinnacle's pre-game quotes) where grading looks for it."""
    from betdata.alerts.db import engine_for

    monkeypatch.setenv("BETDATA_DATA_DIR", str(tmp_path))
    return engine_for(tmp_path / "poller.sqlite")


def pin(poller, game, market, outcome, line, price, at):
    from betdata.alerts.db import quotes

    with poller.begin() as p:
        p.execute(insert(quotes).values(polled_at=at, sport="nfl", game_id=game,
                                        source="the_odds_api", book="pinnacle", market=market,
                                        outcome=outcome, line=line, price=price))  # fmt: skip


def test_grading_cases(db, poller):
    """Picks at DK/FD, best of the two for the side at the prediction's time; CLV against
    Pinnacle's pre-game close, else DK/FD's."""
    with db.begin() as conn:
        phi = gid(conn, "2025_01_DAL_PHI")  # PHI 24-20 (44 pts)
        lac = gid(conn, "2025_01_KC_LAC")  # LAC 27-21
        atl = gid(conn, "1999_01_MIN_ATL")  # ATL 14-17: no DK/FD quote, so no pick
        chi = gid(conn, "2026_06_CHI_ATL")  # not played
        k = kick(conn, phi)
        # DAL: DK +10 at -110, FD +10.5 at -115: the pick is FD's +10.5 (the better number)
        two_way(conn, phi, "draftkings", "spreads", -10.0, -110, -110, k - timedelta(hours=3))
        two_way(conn, phi, "fanduel", "spreads", -10.5, -105, -115, k - timedelta(hours=3))
        # over: DK 48.5, FD 49 -> DK's 48.5; DK closes 47.5 (no Pinnacle total)
        two_way(conn, phi, "draftkings", "totals", 48.5, -110, -110, k - timedelta(hours=3))
        two_way(conn, phi, "fanduel", "totals", 49.0, -110, -110, k - timedelta(hours=3))
        two_way(conn, phi, "draftkings", "totals", 47.5, -110, -110, k - timedelta(minutes=30))
        # Pinnacle closes PHI -8.5 (DAL +8.5): CLV 10.5 - 8.5 = +2 at the Pinnacle close
        pin(poller, phi, "spreads", "home", -8.5, -105, k - timedelta(minutes=20))
        pin(poller, phi, "spreads", "away", 8.5, -105, k - timedelta(minutes=20))
        p1 = predict(conn, phi, 0.0, 55.0, None, None)
        # LAC -6 at DK; a quote after kickoff is neither the pick line nor the close
        k2 = kick(conn, lac)
        two_way(conn, lac, "draftkings", "spreads", -6.0, -110, -110, k2 - timedelta(hours=3))
        two_way(conn, lac, "draftkings", "spreads", -3.0, -110, -110, k2 + timedelta(hours=1))
        p2 = predict(conn, lac, 10.0, None, None, None)
        p3 = predict(conn, atl, -10.0, None, -4.0, None)  # nflverse line only: no pick
        conn.execute(update(games).where(games.c.game_id == chi).values(status="postponed"))
        two_way(conn, chi, "fanduel", "spreads", -1.5, -110, -110,
                kick(conn, chi) - timedelta(hours=3))  # fmt: skip
        p4 = predict(conn, chi, 10.0, None, None, None)
        run = grade_nfl(conn, NOW)
        g = grades(conn)
    assert (run.inserted, run.updated) == (4, 0)
    s1 = g[(p1, "spread")]
    assert (s1.side, s1.line, s1.book, s1.price_american) == ("away", 10.5, "fanduel", -115)
    assert (s1.result, s1.clv, s1.close_line, s1.close_book) == ("win", 2.0, 8.5, "pinnacle")
    assert s1.profit == pytest.approx(100 / 115, rel=1e-4)
    t1 = g[(p1, "total")]
    assert (t1.side, t1.line, t1.book, t1.result) == ("over", 48.5, "draftkings", "loss")
    assert (t1.clv, t1.close_book) == (-1.0, "draftkings")
    s2 = g[(p2, "spread")]
    assert (s2.line, s2.result, s2.close_line, s2.clv) == (-6.0, "push", -6.0, 0.0)
    assert not any(pid == p3 for pid, _ in g)
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
    assert (spread.wins, spread.losses, spread.pushes, spread.voids) == (2, 0, 0, 1)
    assert spread.avg_clv == pytest.approx((2.0 + 0.0) / 2) and spread.ties == 1


def test_a_pick_that_no_longer_qualifies_is_regraded_away(db, poller):
    """Grades stored under the old rule (any book's line on the prediction) are replaced:
    with no DK/FD quote at pick time there is no pick."""
    with db.begin() as conn:
        phi = gid(conn, "2025_01_DAL_PHI")
        p = predict(conn, phi, 0.0, None, -10.0, None)
        conn.execute(insert(nfl_bet_grades).values(
            prediction_id=p, model_version="m12:test", game_id=phi, market="spread",
            side="away", line=10.0, price_american=-110, edge=10.0, result="win",
            profit=0.9, graded_at=NOW))  # fmt: skip
        run = grade_nfl(conn, NOW)
        assert run.updated == 1 and grades(conn) == {}


def test_only_the_last_prediction_before_kickoff_counts(db):
    with db.begin() as conn:
        phi = gid(conn, "2025_01_DAL_PHI")
        kickoff = kick(conn, phi)
        two_way(conn, phi, "draftkings", "spreads", -8.5, -110, -110, kickoff - timedelta(hours=3))
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
        phi = gid(conn, "2025_01_DAL_PHI")
        two_way(conn, phi, "draftkings", "spreads", -8.5, -110, -110,
                kick(conn, phi) - timedelta(hours=3))  # fmt: skip
        predict(conn, phi, 0.0, None, -8.5, None)
    src = NflModelSource(db, settings.source("nfl_model"), clock=clock)
    result = src.run("grade")
    assert (result.status, result.rows_inserted) == ("ok", 1)
    assert src.run("grade").rows_inserted == 0  # idempotent
    with db.connect() as conn:
        assert conn.execute(select(func.count()).select_from(ingest_runs)).scalar() >= 3


def test_moneyline_grading(db, poller):
    from betdata.moneyline import no_vig
    from betdata.nfl.grade import ml_calibration, ml_threshold, moneyline_bet

    t = ml_threshold()
    assert moneyline_bet(0.5, -110, -110, t) is None  # no edge
    assert moneyline_bet(None, -110, -110, t) is None and moneyline_bet(0.7, None, 120, t) is None
    b = moneyline_bet(0.62, -110, -110, t)
    assert (b.market, b.side, b.price, b.line, b.edge) == ("moneyline", "home", -110, None, 12.0)
    assert result_of(b, 20, 20) == "push"

    with db.begin() as conn:
        phi = gid(conn, "2025_01_DAL_PHI")  # PHI 24-20
        lac = gid(conn, "2025_01_KC_LAC")  # LAC 27-21
        k, k2 = kick(conn, phi), kick(conn, lac)
        # DAL: DK +320, FD +330 -> FD (the larger edge); Pinnacle closes DAL +335
        two_way(conn, phi, "draftkings", "h2h", None, -400, 320, k - timedelta(hours=3))
        two_way(conn, phi, "fanduel", "h2h", None, -420, 330, k - timedelta(hours=3))
        pin(poller, phi, "h2h", "home", None, -440, k - timedelta(minutes=10))
        pin(poller, phi, "h2h", "away", None, 335, k - timedelta(minutes=10))
        # LAC +160 at DK only; no Pinnacle: DK's close (+145) is the close
        two_way(conn, lac, "draftkings", "h2h", None, 160, -190, k2 - timedelta(hours=3))
        two_way(conn, lac, "draftkings", "h2h", None, 145, -175, k2 - timedelta(minutes=30))
        p1 = predict(conn, phi, 0.0, None, None, None)
        p2 = predict(conn, lac, 0.0, None, None, None)
        for pid, prob in ((p1, 0.30), (p2, 0.70)):
            conn.execute(update(model_predictions).where(model_predictions.c.prediction_id == pid)
                         .values(pred_home_win_prob=prob))  # fmt: skip
        grade_nfl(conn, NOW)
        g = grades(conn)
        recs = {(r.model_version, r.market): r for r in records(conn)}
    calib = ml_calibration()
    dk, fd = moneyline_bet(0.30, -400, 320, t, calib), moneyline_bet(0.30, -420, 330, t, calib)
    assert fd.edge > dk.edge
    dal = g[(p1, "moneyline")]  # backed DAL at FD +330; DAL lost
    assert (dal.side, dal.book, dal.price_american, dal.result, dal.profit) == (
        "away", "fanduel", 330, "loss", -1.0)
    assert (dal.close_book, dal.close_price_american, dal.line) == ("pinnacle", 335, None)
    want = round(100 * (no_vig(-440, 335)[1] - no_vig(-420, 330)[1]), 2)
    assert dal.clv == pytest.approx(want)
    lac_g = g[(p2, "moneyline")]  # backed LAC at +160; LAC won; closed +145 (beat the close)
    assert (lac_g.side, lac_g.result, lac_g.profit, lac_g.close_book) == (
        "home", "win", 1.6, "draftkings")
    assert lac_g.clv > 0
    rec = recs[("m12:test", "moneyline")]
    assert (rec.wins, rec.losses) == (1, 1)
