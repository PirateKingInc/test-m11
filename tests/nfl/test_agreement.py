"""The spread agreement rule (agreement.py): a threshold pick is flagged only when the DK/FD
line moved toward the side since its first quote, or Pinnacle's no-vig line sides with the
model against the DK/FD number. Grading keeps every threshold pick and records the signals."""

from datetime import timedelta

from sqlalchemy import select, update

from betdata.agreement import MOVE, PINNACLE, card_agreement, spread_agreement
from betdata.db.schema import model_predictions, nfl_bet_grades
from betdata.nfl.book_lines import Board
from betdata.nfl.grade import AGREEMENT_MARKET, grade_nfl, records
from betdata.sports import Card
from tests.nfl.test_grade import NOW, gid, kick, pin, predict, two_way


def test_line_move_toward_the_side(db):
    with db.begin() as conn:
        phi = gid(conn, "2025_01_DAL_PHI")
        k = kick(conn, phi)
        two_way(conn, phi, "draftkings", "spreads", -3.0, -110, -110, k - timedelta(days=3))
        two_way(conn, phi, "draftkings", "spreads", -3.5, -110, -110, k - timedelta(hours=3))
        b = Board(conn, [phi])
        at = k - timedelta(hours=2)
        home = spread_agreement(b, conn, None, "nfl", phi, "home", at, "PHI", "draftkings", -3.5)
        away = spread_agreement(b, conn, None, "nfl", phi, "away", at, "DAL", "draftkings", 3.5)
        # before the second quote there is no move yet
        early = spread_agreement(b, conn, None, "nfl", phi, "home", k - timedelta(days=2),
                                 "PHI", "draftkings", -3.0)  # fmt: skip
    assert home.signals == (MOVE,) and "PHI -3 → -3.5" in home.notes[0] and home.code == "move"
    assert not away and away.code == ""
    assert not early


def test_the_other_book_counts_only_when_the_picks_book_has_no_history(db):
    with db.begin() as conn:
        phi = gid(conn, "2025_01_DAL_PHI")
        k = kick(conn, phi)
        two_way(conn, phi, "fanduel", "spreads", -2.5, -110, -110, k - timedelta(days=3))
        two_way(conn, phi, "fanduel", "spreads", -3.5, -110, -110, k - timedelta(hours=4))
        two_way(conn, phi, "draftkings", "spreads", -3.0, -110, -110, k - timedelta(hours=3))
        b = Board(conn, [phi])
        a = spread_agreement(b, conn, None, "nfl", phi, "home", k - timedelta(hours=2), "PHI",
                             "draftkings", -3.0)  # fmt: skip
    assert a.signals == (MOVE,) and a.notes[0].startswith("FD line moved toward PHI")


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


def test_card_flags_only_with_agreement(db, poller):
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
    assert "Spread: PHI" not in html and "not flagged" in html


def test_grading_records_agreement_and_a_separate_record_row(db, poller):
    with db.begin() as conn:
        phi = gid(conn, "2025_01_DAL_PHI")  # PHI 24-20
        lac = gid(conn, "2025_01_KC_LAC")  # LAC 27-21
        k, k2 = kick(conn, phi), kick(conn, lac)
        # PHI pick (margin 10 vs -3.5 -> home) with the DK line moving toward PHI: agreement
        two_way(conn, phi, "draftkings", "spreads", -3.0, -110, -110, k - timedelta(days=2))
        two_way(conn, phi, "draftkings", "spreads", -3.5, -110, -110, k - timedelta(hours=3))
        p1 = predict(conn, phi, 10.0, None, None, None)
        # LAC pick (home) with no move and no Pinnacle: a threshold pick without agreement
        two_way(conn, lac, "draftkings", "spreads", -2.0, -110, -110, k2 - timedelta(hours=3))
        p2 = predict(conn, lac, 10.0, None, None, None)
        grade_nfl(conn, NOW)
        g = {r.prediction_id: r for r in conn.execute(select(nfl_bet_grades))}
        recs = {(r.model_version, r.market): r for r in records(conn)}
    assert (g[p1].agree, g[p1].result) == ("move", "win")  # PHI by 4 covers -3.5
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
