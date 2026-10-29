"""M18 Part G1: unders in each team's first four home games when the closing total is at least
0.5 below the open; the backtest's selection and grading, and the live paper check."""

from datetime import UTC, timedelta

import httpx
import pandas as pd
import pytest
from sqlalchemy import insert, select

from betdata.alerts.db import alerts, engine_for
from betdata.alerts.feeds import Http, Quote
from betdata.alerts.poller import Keys, Poller
from betdata.db.schema import books, games, markets, odds_snapshots, results
from betdata.nba import g1 as G
from tests.nba.synth import synth


def _totals(conn, gid, open_, close, under=-110):
    book = conn.execute(select(books.c.book_id).where(books.c.key == "draftkings")).scalar()
    mid = conn.execute(select(markets.c.market_id).where(markets.c.key == "totals",
                                                         markets.c.period == "game")).scalar()
    tip = conn.execute(select(games.c.start_time_utc).where(games.c.game_id == gid)).scalar()
    for kind, line, at in (("source_opening", open_, tip - timedelta(days=1)),
                           ("source_closing", close, tip - timedelta(minutes=5))):
        for outcome in ("over", "under"):
            conn.execute(insert(odds_snapshots).values(
                source="espn_nba", source_game_id=str(gid), game_id=gid, book_id=book,
                market_id=mid, outcome=outcome, line=line,
                price_american=under if outcome == "under" else -110, snapshot_kind=kind,
                fetched_at=at))  # fmt: skip


def test_backtest_selects_and_grades(engine, tmp_path, monkeypatch):
    ids = synth(engine, n_days=30)
    with engine.connect() as c:
        early = G.early_home_games(c)
    assert (early.groupby("home_team_id").size() <= 4).all() and len(early) == 16
    first = early.sort_values("tip").game_id.tolist()
    later = [g for g in ids if g not in set(first)][0]
    with engine.begin() as c:
        tot = dict(c.execute(select(results.c.game_id, results.c.home_score
                                    + results.c.away_score)).all())  # fmt: skip
        g_drop, g_flat, g_small = first[0], first[1], first[2]
        _totals(c, g_drop, tot[g_drop] + 5.5, tot[g_drop] + 4.5)  # dropped 1: under wins
        _totals(c, g_flat, 220.0, 220.0)  # no drop: no bet
        _totals(c, g_small, tot[g_small] - 3.0, tot[g_small] - 3.5, under=120)  # 0.5: loses
        _totals(c, later, 250.0, 240.0)  # not a first-four home game
    monkeypatch.setattr(G, "REPORT", tmp_path / "g1.json")
    with engine.connect() as c:
        d = G.picks(c)
        rep = G.backtest(c)
    assert sorted(d.game_id) == sorted([g_drop, g_small])
    assert dict(zip(d.game_id, d.result, strict=True)) == {g_drop: "win", g_small: "loss"}
    assert rep["all"]["n"] == 2 and rep["all"]["w"] == 1 and rep["all"]["l"] == 1
    assert rep["all"]["roi"] == pytest.approx((100 / 110 - 1) / 2, abs=1e-3)
    assert rep["candidates_with_lines"] == 3 and (tmp_path / "g1.json").exists()


def test_live_check_alerts_the_under_once(engine, tmp_path, monkeypatch):
    synth(engine, n_days=30)
    with engine.connect() as c:
        early = G.early_home_games(c)
        gid = int(early.sort_values("tip").game_id.iloc[0])
        tip = c.execute(select(games.c.start_time_utc).where(games.c.game_id == gid)).scalar()
    tip = tip if tip.tzinfo else tip.replace(tzinfo=UTC)
    pol = Poller(engine, engine_for(tmp_path / "poller.sqlite"), Keys("sk", None, None),
                 http=Http(transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                           sleep=lambda s: None),
                 now_fn=lambda: tip, lock=lambda: False)  # fmt: skip
    monkeypatch.setattr("betdata.core.matching.season_for", lambda sport, now: 2021)

    def tq(line, op, up):
        return [Quote("nba", gid, "sharpapi", "draftkings", "totals", "over", line, op),
                Quote("nba", gid, "sharpapi", "draftkings", "totals", "under", line, up)]

    pol.store_quotes(tq(224.5, -110, -110), tip - timedelta(days=1))  # the opener
    monkeypatch.setattr(pol, "poll_game", lambda sport, g, now, ev: tq(223.5, -105, -115))
    now = tip - timedelta(minutes=10)
    assert pol.has_work(now - timedelta(minutes=2), now)
    assert len(pol.g1_check(now)) == 1
    assert pol.g1_check(now + timedelta(minutes=1)) == []  # once per game
    with pol.pdb.connect() as p:
        a = p.execute(select(alerts)).one()
    assert (a.signal, a.side, a.line, a.price, a.study) == ("g1", "under", 223.5, -115, True)
    assert "opened 224.5" in a.reason
    assert isinstance(pd.Timestamp(a.tip), pd.Timestamp)
