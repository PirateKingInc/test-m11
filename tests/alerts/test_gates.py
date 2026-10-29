"""M18 Part H: alerts graded against the consensus close (the last poll before tip), CLV at the
alerted line, and the 60 / 200 gates per signal."""

from datetime import UTC, timedelta

import pytest
from sqlalchemy import insert, select

from betdata.alerts.db import alerts, consensus_grades, engine_for
from betdata.alerts.feeds import Quote
from betdata.alerts.gates import EARLY_N, GATE_N, gate, gates, grade_consensus
from betdata.alerts.poller import Keys, Poller
from betdata.db.schema import games
from betdata.moneyline import no_vig
from tests.nba.synth import synth


@pytest.fixture
def setup(engine, tmp_path):
    gid = synth(engine, n_days=1)[0]
    with engine.connect() as c:
        tip = c.execute(select(games.c.start_time_utc).where(games.c.game_id == gid)).scalar()
    tip = tip if tip.tzinfo else tip.replace(tzinfo=UTC)
    pol = Poller(engine, engine_for(tmp_path / "poller.sqlite"), Keys(None, None, None),
                 now_fn=lambda: tip, lock=lambda: False)  # fmt: skip
    return pol, gid, tip


def _alert(pol, gid, tip, market, side, line, book_prob, player=None, signal="value", key="k"):
    with pol.pdb.begin() as p:
        return p.execute(insert(alerts).values(
            signal=signal, sport="nba", game_id=gid, game_label="A @ B", tip=tip, market=market,
            player_name=player, side=side, line=line, price=-110, book="draftkings",
            book_prob=book_prob, reason="r", dedup_key=key, created_at=tip - timedelta(hours=3),
            quiet=False, sent=False)).inserted_primary_key[0]  # fmt: skip


def test_clv_against_the_consensus_close(setup, engine):
    pol, gid, tip = setup
    a1 = _alert(pol, gid, tip, "spreads", "away", 4.5, 0.50, key="s")
    a2 = _alert(pol, gid, tip, "rebounds", "over", 7.5, 0.48, "LeBron James", key="p")
    # the board 5 minutes before tip: Pinnacle home -3.5 at -105/-105; the prop at 7.5 (FD/DK)
    pol.store_quotes([Quote("nba", gid, "the_odds_api", "pinnacle", "spreads", "home", -3.5,
                            -105),
                      Quote("nba", gid, "the_odds_api", "pinnacle", "spreads", "away", 3.5,
                            -105),
                      Quote("nba", gid, "sharpapi", "draftkings", "rebounds", "over", 7.5, -130,
                            "LeBron James"),
                      Quote("nba", gid, "sharpapi", "draftkings", "rebounds", "under", 7.5, 100,
                            "LeBron James")], tip - timedelta(minutes=5))  # fmt: skip
    # a quote after tip is ignored
    pol.store_quotes([Quote("nba", gid, "the_odds_api", "pinnacle", "spreads", "home", -9.5,
                            -105)], tip + timedelta(minutes=5))  # fmt: skip
    assert grade_consensus(engine, pol.pdb, tip + timedelta(hours=3)) == 2
    with pol.pdb.connect() as p:
        g = {r.alert_id: r for r in p.execute(select(consensus_grades))}
    from statistics import NormalDist

    # away +4.5 vs a fair home line of -3.5: P(away covers +4.5) = 1 - Phi((-4.5 + 3.5) / 12)
    want = 1 - NormalDist().cdf((-4.5 + 3.5) / 12)
    assert g[a1].close_method == "pinnacle" and g[a1].close_prob == pytest.approx(want)
    assert g[a1].clv == pytest.approx(want - 0.50, abs=1e-4)
    assert g[a2].close_two_book and g[a2].close_line == 7.5
    assert g[a2].clv == pytest.approx(no_vig(-130, 100)[0] - 0.48, abs=1e-4)
    with pol.pdb.connect() as p:
        gs = gates(p)
    assert gs["C-NBA"]["alerts"] == 2 and gs["C-NBA"]["n"] == 2


def test_gate_thresholds():
    assert gate([0.02] * (EARLY_N - 1))["status"].startswith("no read before 60")
    early = gate([0.03, 0.01] * (EARLY_N // 2))
    assert early["status"] == "early read: positive, range excludes 0"
    assert gate([0.03, -0.03] * (EARLY_N // 2))["status"] == "early read: range includes 0"
    assert gate([0.03, 0.01] * (GATE_N // 2))["status"] == "working"
    assert gate([0.04, -0.035] * (GATE_N // 2))["status"] == "not working"
