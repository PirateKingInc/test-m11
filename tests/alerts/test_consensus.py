"""M18 Part B, the consensus price: Pinnacle when fresh, else the median of fresh books on a
common scale; "two-book" labelling; props at one line; freshness over change-only storage
(poll marks, removal rows); stored with inputs."""

from datetime import UTC, datetime, timedelta
from statistics import NormalDist

import pytest
from sqlalchemy import select

from betdata.alerts import consensus as C
from betdata.alerts.db import consensus, engine_for
from betdata.alerts.feeds import Quote
from betdata.alerts.poller import Keys, Poller
from betdata.moneyline import no_vig

T = datetime(2026, 11, 1, 20, 0, tzinfo=UTC)
G = 7
N = NormalDist()


@pytest.fixture
def pol(engine, tmp_path):
    return Poller(engine, engine_for(tmp_path / "poller.sqlite"), Keys(None, None, None),
                  now_fn=lambda: T, lock=lambda: False)  # fmt: skip


def spread(book, home_line, hp, ap, source="sharpapi"):
    return [Quote("nba", G, source, book, "spreads", "home", home_line, hp),
            Quote("nba", G, source, book, "spreads", "away", -home_line, ap)]


def prop(book, line, op, up, name="LeBron James", market="rebounds"):
    return [Quote("nba", G, "sharpapi", book, market, "over", line, op, name),
            Quote("nba", G, "sharpapi", book, market, "under", line, up, name)]


def fair_line(line, hp, ap, sig=12.0):
    return line - sig * N.inv_cdf(no_vig(hp, ap)[0])


def test_pinnacle_wins_when_fresh(pol, engine):
    pol.store_quotes(spread("draftkings", -3.5, -110, -110) + spread("fanduel", -4.0, -110, -110),
                     T - timedelta(minutes=2))
    pol.store_quotes(spread("pinnacle", -4.5, -105, -105, "the_odds_api"), T - timedelta(minutes=8))
    with pol.pdb.connect() as p, engine.connect() as m:
        c = C.game_consensus(p, m, "nba", G, "spreads", T)
        stale = C.game_consensus(p, m, "nba", G, "spreads", T + timedelta(minutes=3))
    assert (c.method, c.n, c.two_book) == ("pinnacle", 1, False)
    assert c.fair == pytest.approx(-4.5)
    assert c.prob("home", -4.5) == pytest.approx(0.5)
    assert c.prob("home", -3.5) > 0.5 and c.prob("away", 3.5) < 0.5
    # Pinnacle is 11 minutes old 3 minutes later: the median of DK/FD, two-book
    assert (stale.method, stale.n, stale.two_book) == ("median", 2, True)
    assert stale.fair == pytest.approx(-3.75)


def test_median_on_the_fair_scale(pol, engine):
    pol.store_quotes(spread("draftkings", -3.5, -125, 105) + spread("fanduel", -4.0, -110, -110)
                     + spread("betmgm", -3.0, -140, 120, "the_odds_api"), T)
    with pol.pdb.connect() as p, engine.connect() as m:
        c = C.game_consensus(p, m, "nba", G, "spreads", T)
    want = sorted([fair_line(-3.5, -125, 105), fair_line(-4.0, -110, -110),
                   fair_line(-3.0, -140, 120)])[1]
    assert (c.method, c.n, c.two_book) == ("median", 3, False) and c.fair == pytest.approx(want)
    # carried to another line: P(home covers L) = Phi((L - L*) / sigma)
    assert c.prob("home", -5.0) == pytest.approx(N.cdf((-5.0 - want) / 12.0))
    assert c.prob("away", 5.0) == pytest.approx(1 - N.cdf((-5.0 - want) / 12.0))


def test_moneyline_kalshi_votes_and_totals(pol, engine):
    pol.store_quotes([Quote("nba", G, "sharpapi", "draftkings", "h2h", "home", None, -150),
                      Quote("nba", G, "sharpapi", "draftkings", "h2h", "away", None, 130),
                      Quote("nba", G, "kalshi", "kalshi", "h2h", "home", None, -170),
                      Quote("nba", G, "kalshi", "kalshi", "h2h", "away", None, 150),
                      Quote("nba", G, "sharpapi", "fanduel", "totals", "over", 221.5, -110),
                      Quote("nba", G, "sharpapi", "fanduel", "totals", "under", 221.5, -110)],
                     T)  # fmt: skip
    with pol.pdb.connect() as p, engine.connect() as m:
        ml = C.game_consensus(p, m, "nba", G, "h2h", T)
        tot = C.game_consensus(p, m, "nba", G, "totals", T)
    want = (no_vig(-150, 130)[0] + no_vig(-170, 150)[0]) / 2
    assert (ml.n, ml.two_book) == (2, True) and ml.fair == pytest.approx(want)
    assert ml.prob("away") == pytest.approx(1 - want)
    assert tot.fair == pytest.approx(221.5) and tot.prob("over", 220.5) > 0.5


def test_props_one_line_and_two_book(pol):
    pol.store_quotes(prop("draftkings", 7.5, -120, -110) + prop("fanduel", 7.5, -105, -125)
                     + prop("draftkings", 25.5, -110, -110, market="points"), T)
    with pol.pdb.connect() as p:
        c = C.prop_consensus(p, "nba", G, "rebounds", "James, LeBron", T)
        none = C.prop_consensus(p, "nba", G, "rebounds", "LeBron James", T, line=8.5)
    want = (no_vig(-120, -110)[0] + no_vig(-105, -125)[0]) / 2
    assert (c.line, c.n, c.two_book) == (7.5, 2, True) and c.fair == pytest.approx(want)
    assert c.prob("under", 7.5) == pytest.approx(1 - want) and c.prob("over", 8.5) is None
    assert none is None


def test_freshness_survives_change_only_storage_and_removals(pol, engine):
    pol.store_quotes(spread("draftkings", -3.5, -110, -110) + prop("draftkings", 7.5, -115, -105),
                     T - timedelta(minutes=30), change_only=True)
    # 25 minutes later the same prices: no new quote rows, but the poll is marked
    pol.store_quotes(spread("draftkings", -3.5, -110, -110) + prop("draftkings", 7.5, -115, -105),
                     T - timedelta(minutes=5), change_only=True)
    with pol.pdb.connect() as p, engine.connect() as m:
        c = C.game_consensus(p, m, "nba", G, "spreads", T)
        pc = C.prop_consensus(p, "nba", G, "rebounds", "LeBron James", T)
    assert c is not None and c.fair == pytest.approx(-3.5)
    assert c.inputs[0].polled_at == T - timedelta(minutes=5)
    assert pc is not None and pc.line == 7.5
    # the prop is pulled at the next poll: a removal row, so it is no longer on the board
    pol.store_quotes(spread("draftkings", -3.5, -110, -110), T, change_only=True)
    with pol.pdb.connect() as p:
        assert C.prop_consensus(p, "nba", G, "rebounds", "LeBron James", T) is None


def test_sweep_stores_consensus_with_inputs(pol):
    qs = spread("draftkings", -3.5, -110, -110) + prop("fanduel", 7.5, -110, -110)
    pol.store_quotes(qs, T, change_only=True)
    assert pol.store_consensus("nba", G, qs, T) == 2
    with pol.pdb.connect() as p:
        rows = p.execute(select(consensus).order_by(consensus.c.consensus_id)).all()
    assert [(r.market, r.method, r.two_book, r.n) for r in rows] == [
        ("spreads", "median", True, 1), ("rebounds", "median", True, 1)]
    assert rows[0].inputs[0]["book"] == "draftkings" and rows[1].line == 7.5
