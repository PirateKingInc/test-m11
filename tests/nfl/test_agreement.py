"""Independent votes on a model spread pick (agreement.py, M18 Part A): Pinnacle's no-vig line
sides with the model against the DK/FD number, or Kalshi's fresh win probability beats DK/FD's
no-vig. Model picks are research only (never flagged); grading records the votes."""

from datetime import timedelta

from sqlalchemy import insert, select, update

from betdata.agreement import KALSHI, PINNACLE, card_agreement, spread_agreement
from betdata.db.schema import model_predictions, nfl_bet_grades
from betdata.nfl.book_lines import Board
from betdata.nfl.grade import AGREEMENT_MARKET, grade_nfl, records
from betdata.sports import Card
from tests.nfl.test_grade import NOW, gid, kick, pin, predict, two_way


def kalshi(poller, game, outcome, price, at):
    from betdata.alerts.db import quotes

    with poller.begin() as p:
        p.execute(insert(quotes).values(polled_at=at, sport="nfl", game_id=game, source="kalshi",
                                        book="kalshi", market="h2h", outcome=outcome, line=None,
                                        price=price))  # fmt: skip


def test_a_line_move_alone_is_no_longer_a_vote(db):
    with db.begin() as conn:
        phi = gid(conn, "2025_01_DAL_PHI")
        k = kick(conn, phi)
        two_way(conn, phi, "draftkings", "spreads", -3.0, -110, -110, k - timedelta(days=3))
        two_way(conn, phi, "draftkings", "spreads", -3.5, -110, -110, k - timedelta(hours=3))
        a = spread_agreement(Board(conn, [phi]), conn, None, "nfl", phi, "home",
                             k - timedelta(hours=2), "PHI", "draftkings", -3.5)  # fmt: skip
    assert not a and a.code == ""


def test_kalshi_votes_when_fresh_and_above_dkfd_no_vig(db, poller):
    with db.begin() as conn:
        phi = gid(conn, "2025_01_DAL_PHI")
        k = kick(conn, phi)
        at = k - timedelta(hours=2)
        two_way(conn, phi, "draftkings", "spreads", -3.0, -110, -110, k - timedelta(hours=3))
        two_way(conn, phi, "draftkings", "h2h", None, -160, 135, k - timedelta(hours=3))
        kalshi(poller, phi, "home", -190, at - timedelta(minutes=5))  # PHI 65.5% vs ~59%
        kalshi(poller, phi, "away", 160, at - timedelta(minutes=5))
        b = Board(conn, [phi])
        with poller.connect() as p:
            home = spread_agreement(b, conn, p, "nfl", phi, "home", at, "PHI", "draftkings", -3.0)
            away = spread_agreement(b, conn, p, "nfl", phi, "away", at, "DAL", "draftkings", 3.0)
            stale = spread_agreement(b, conn, p, "nfl", phi, "home", at + timedelta(minutes=20),
                                     "PHI", "draftkings", -3.0)  # fmt: skip
    assert home.signals == (KALSHI,) and home.code == "kalshi" and "Kalshi" in home.notes[0]
    assert not away and not stale


def test_pinnacle_sides_with_the_model(db, poller):
    with db.begin() as conn:
        phi = gid(conn, "2025_01_DAL_PHI")
        k = kick(conn, phi)
        two_way(conn, phi, "draftkings", "spreads", -3.0, -110, -110, k - timedelta(hours=3))
        # Pinnacle PHI -3.5 at -105/-105: carried to DK's -3, PHI covers more than half
        pin(poller, phi, "spreads", "home", -3.5, -105, k - timedelta(hours=3))
        pin(poller, phi, "spreads", "away", 3.5, -105, k - timedelta(hours=3))
        b, at = Board(conn, [phi]), k - timedelta(hours=2)
        with poller.connect() as p:
            home = spread_agreement(b, conn, p, "nfl", phi, "home", at, "PHI", "draftkings",
                                    -3.0)  # fmt: skip
            away = spread_agreement(b, conn, p, "nfl", phi, "away", at, "DAL", "draftkings",
                                    3.0)  # fmt: skip
            # a Pinnacle quote after the pick time does not count
            later = spread_agreement(b, conn, p, "nfl", phi, "home", k - timedelta(hours=4),
                                     "PHI", "draftkings", -3.0)  # fmt: skip
    assert home.signals == (PINNACLE,) and "PHI -3.5" in home.notes[0]
    assert not away and not later


def test_card_never_flags_a_model_pick(db, poller):
    with db.begin() as conn:
        phi = gid(conn, "2025_01_DAL_PHI")
        k = kick(conn, phi)
        two_way(conn, phi, "draftkings", "spreads", -3.0, -110, -110, k - timedelta(hours=3))
        b = Board(conn, [phi])
        q = b.quote(phi, "spreads", "draftkings", "home", k)
        from betdata.quotes import Quote

        c = Card(phi, k, "PHI", "DAL", "prekick", k, spread_pick="PHI",
                 market_spread=Quote(q.line, q.price, q.book, q.source, q.fetched_at))
        card_agreement(c, b, conn, None, "nfl", k - timedelta(hours=1), dkfd_line=True)
    assert c.spread_agree is not None and not c.spread_agree

    from betdata.page.build import card_html

    html = card_html(c, {}, [])
    assert "Spread: PHI" not in html and 'class="flag"' not in html
    assert "research only" in html and "no Pinnacle or Kalshi vote" in html


def test_grading_records_agreement_and_a_separate_record_row(db, poller):
    with db.begin() as conn:
        phi = gid(conn, "2025_01_DAL_PHI")  # PHI 24-20
        lac = gid(conn, "2025_01_KC_LAC")  # LAC 27-21
        k, k2 = kick(conn, phi), kick(conn, lac)
        # PHI pick (margin 10 vs -3.5 -> home) with Pinnacle siding with PHI: agreement
        two_way(conn, phi, "draftkings", "spreads", -3.5, -110, -110, k - timedelta(hours=3))
        pin(poller, phi, "spreads", "home", -4.5, -105, k - timedelta(hours=3))
        pin(poller, phi, "spreads", "away", 4.5, -105, k - timedelta(hours=3))
        p1 = predict(conn, phi, 10.0, None, None, None)
        # LAC pick (home) with no Pinnacle or Kalshi: a threshold pick without agreement
        two_way(conn, lac, "draftkings", "spreads", -2.0, -110, -110, k2 - timedelta(hours=3))
        p2 = predict(conn, lac, 10.0, None, None, None)
        grade_nfl(conn, NOW)
        g = {r.prediction_id: r for r in conn.execute(select(nfl_bet_grades))}
        recs = {(r.model_version, r.market): r for r in records(conn)}
    assert (g[p1].agree, g[p1].result) == ("pinnacle", "win")  # PHI by 4 covers -3.5
    assert (g[p2].agree, g[p2].result) == ("", "win")  # LAC by 6 covers -2
    allp, agree = recs[("m12:test", "spread")], recs[("m12:test", AGREEMENT_MARKET)]
    assert (allp.wins, allp.losses) == (2, 0) and (agree.wins, agree.losses) == (1, 0)


def test_moneyline_grades_carry_no_agreement(db):
    with db.begin() as conn:
        lac = gid(conn, "2025_01_KC_LAC")
        two_way(conn, lac, "draftkings", "h2h", None, 160, -190,
                kick(conn, lac) - timedelta(hours=3))  # fmt: skip
        p = predict(conn, lac, 0.0, None, None, None)
        conn.execute(update(model_predictions).where(model_predictions.c.prediction_id == p)
                     .values(pred_home_win_prob=0.70))  # fmt: skip
        grade_nfl(conn, NOW)
        r = conn.execute(select(nfl_bet_grades).where(nfl_bet_grades.c.prediction_id == p)).one()
    assert r.market == "moneyline" and r.agree is None
