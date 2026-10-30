"""M18 Part C value rule (alerts/value.py): stale-since-report, the 3 / 6 pp edges, the 1.5x bar
for two-book or model-only prices unless the other book or the consensus already moved, the
prop repricing consensus_before(L) + p_after(L) - p_before(L)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from betdata.alerts import consensus as C
from betdata.alerts import value as V
from betdata.alerts.db import engine_for
from betdata.alerts.feeds import Quote
from betdata.alerts.poller import Keys, Poller
from betdata.moneyline import no_vig

STATED = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)
NOW = STATED + timedelta(minutes=12)
G = 3


@pytest.fixture
def pol(engine, tmp_path):
    return Poller(engine, engine_for(tmp_path / "poller.sqlite"), Keys(None, None, None),
                  now_fn=lambda: NOW, lock=lambda: False)  # fmt: skip


def sp(book, line, hp=-110, ap=-110, source="sharpapi"):
    return [Quote("nba", G, source, book, "spreads", "home", line, hp),
            Quote("nba", G, source, book, "spreads", "away", -line, ap)]


def pr(book, line, op, up, name="Jalen Brunson", market="assists"):
    return [Quote("nba", G, "sharpapi", book, market, "over", line, op, name),
            Quote("nba", G, "sharpapi", book, market, "under", line, up, name)]


def rp(mb, ma):
    return SimpleNamespace(margin_before=mb, margin_after=ma, total_before=None, total_after=None)


def run(pol, engine, market, rep, now_qs, name=None):
    with pol.pdb.connect() as p, engine.connect() as m:
        return V.evaluate(p, m, "nba", G, market, name, rep, now_qs, STATED, NOW)


def cons_at(pol, engine, market, t):
    with pol.pdb.connect() as p, engine.connect() as m:
        return C.game_consensus(p, m, "nba", G, market, t)


def test_pinnacle_consensus_uses_the_3pp_bar(pol, engine):
    pre = STATED - timedelta(minutes=5)
    pol.store_quotes(sp("draftkings", -3.0) + sp("pinnacle", -3.0, source="the_odds_api"), pre)
    cons = cons_at(pol, engine, "spreads", pre)
    assert cons.method == "pinnacle" and not cons.two_book
    # margin moves 1.2 points to home: L* -3 -> -4.2; P(home -3) = Phi(1.2/12) = 54.0%: 4 pp
    rep = V.game_repricing("nba", "spreads", cons, rp(3.0, 4.2))
    c = run(pol, engine, "spreads", rep, sp("draftkings", -3.0))
    assert c is not None and (c.side, c.book, c.line, c.confirmed) == ("home", "draftkings",
                                                                       -3.0, None)
    assert c.edge == pytest.approx(0.0398, abs=1e-3) and not c.solo


def test_two_book_needs_confirmation_or_4_5pp(pol, engine):
    pre = STATED - timedelta(minutes=5)
    pol.store_quotes(sp("draftkings", -3.0) + sp("fanduel", -3.0), pre)
    cons = cons_at(pol, engine, "spreads", pre)
    rep = V.game_repricing("nba", "spreads", cons, rp(3.0, 4.2))  # 4 pp: below 4.5
    assert run(pol, engine, "spreads", rep, sp("draftkings", -3.0) + sp("fanduel", -3.0)) is None
    # FanDuel has already moved toward home (-3 -> -3.5) since the report: confirmed
    c = run(pol, engine, "spreads", rep, sp("draftkings", -3.0) + sp("fanduel", -3.5))
    assert c is not None and c.book == "draftkings"
    assert c.confirmed == "fanduel moved toward the side"
    # a bigger move clears 4.5 pp alone
    rep = V.game_repricing("nba", "spreads", cons, rp(3.0, 5.0))
    c = run(pol, engine, "spreads", rep, sp("draftkings", -3.0) + sp("fanduel", -3.0))
    assert c is not None and c.solo and c.edge >= 0.045 and c.confirmed is None


def test_not_stale_or_no_pre_report_quote_means_no_alert(pol, engine):
    pol.store_quotes(sp("draftkings", -3.0), STATED - timedelta(minutes=5))
    pol.store_quotes(sp("draftkings", -4.0), STATED + timedelta(minutes=5))  # moved after
    rep = V.game_repricing("nba", "spreads", None, rp(3.0, 9.0))  # model only, huge edge
    assert run(pol, engine, "spreads", rep, sp("draftkings", -4.0)) is None
    assert run(pol, engine, "spreads", rep, sp("fanduel", -4.0)) is None  # no FD quote before


def test_model_only_spread(pol, engine):
    pol.store_quotes(sp("draftkings", -3.0), STATED - timedelta(minutes=50))
    rep = V.game_repricing("nba", "spreads", None, rp(3.0, 8.0))  # model L* = -8
    c = run(pol, engine, "spreads", rep, sp("draftkings", -3.0))
    assert c is not None and c.cons_before is None and c.solo and c.side == "home"


def test_prop_repricing(pol, engine):
    pre = STATED - timedelta(minutes=5)
    pol.store_quotes(pr("draftkings", 6.5, -110, -110) + pr("fanduel", 6.5, -115, -105), pre)
    with pol.pdb.connect() as p:
        before = C.prop_consensus(p, "nba", G, "assists", "Jalen Brunson", pre, 6.5)
    base = before.fair
    rep = V.Repriced("nba", "assists", prop_cons=lambda ln: before if ln == 6.5 else None,
                     prop_p_before=lambda ln: 0.50, prop_p_after=lambda ln: 0.62)
    c = run(pol, engine, "assists", rep, pr("draftkings", 6.5, -110, -110)
            + pr("fanduel", 6.5, -115, -105), name="Brunson, Jalen")  # fmt: skip
    # fair = consensus 50.9% + 12 pp; DK no-vig 50%: edge ~12.9 pp (two-book: above 9)
    assert c is not None and (c.side, c.book) == ("over", "draftkings")
    assert c.fair == pytest.approx(base + 0.12)
    assert c.edge == pytest.approx(base + 0.12 - no_vig(-110, -110)[0])
    # a 7 pp model move on a two-book consensus: under 9 pp and nothing moved -> no alert
    rep.prop_p_after = lambda ln: 0.565
    assert run(pol, engine, "assists", rep, pr("draftkings", 6.5, -110, -110)
               + pr("fanduel", 6.5, -115, -105), name="Jalen Brunson") is None  # fmt: skip


def test_edge_source_tag(pol, engine):
    """Ledger tag: "news" when the repricer's move supplies >= half the edge, else "book
    disagreement" (the stale book was already off the consensus before the news)."""
    pre = STATED - timedelta(minutes=5)
    pol.store_quotes(sp("draftkings", -3.0) + sp("pinnacle", -3.0, source="the_odds_api"), pre)
    rep = V.game_repricing("nba", "spreads", cons_at(pol, engine, "spreads", pre), rp(3.0, 4.2))
    c = run(pol, engine, "spreads", rep, sp("draftkings", -3.0))
    assert c.news_move == pytest.approx(c.edge, abs=1e-6)  # all of it is the news
    assert V.edge_source(c.news_move, c.edge) == "news"


def test_book_disagreement_tag(pol, engine):
    pre = STATED - timedelta(minutes=5)
    # DK -3 vs FD -6: the two-book median L* is -4.5, so DK's -3 is already cheap for home
    pol.store_quotes(sp("draftkings", -3.0) + sp("fanduel", -6.0), pre)
    rep = V.game_repricing("nba", "spreads", cons_at(pol, engine, "spreads", pre), rp(3.0, 3.1))
    c = run(pol, engine, "spreads", rep, sp("draftkings", -3.0) + sp("fanduel", -6.0))
    assert c is not None and c.side == "home" and c.edge >= 0.045
    assert c.news_move < c.edge / 2 and V.edge_source(c.news_move, c.edge) == "book disagreement"


def test_model_only_base_is_the_model_before_the_news():
    r = V.game_repricing("nba", "spreads", None, rp(3.0, 5.0))
    assert r.model_fair_before == pytest.approx(-3.0) and r.model_fair == pytest.approx(-5.0)
    assert r.base("home", -3.0) == pytest.approx(0.5)
