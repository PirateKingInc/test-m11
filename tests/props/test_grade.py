"""M15 prop grading (verification g): over, under, push, an inactive player (void), a missing
closing line, CLV at a moved and at an unchanged line, and idempotence."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update

from betdata.db.schema import books, games, nba_player_games, prop_grades, prop_lines
from betdata.db.schema import prop_predictions as pp
from betdata.nba.props import grade as G
from betdata.props.grade import records
from tests.nba.synth import synth

NOW = datetime(2022, 1, 1, tzinfo=UTC)


@pytest.fixture
def setup(engine, monkeypatch):
    ids = synth(engine, n_days=3)
    monkeypatch.setattr(G, "thresholds", lambda: {"points": 3.0, "rebounds": 3.0})
    with engine.begin() as conn:
        gid = ids[0]
        tip = conn.execute(select(games.c.start_time_utc).where(games.c.game_id == gid)).scalar()
        box = {r.player_id: r for r in conn.execute(
            select(nba_player_games).where(nba_player_games.c.game_id == gid))}  # fmt: skip
        book = conn.execute(select(books.c.book_id).where(books.c.key == "draftkings")).scalar()
    return gid, tip, box, book


def _pred(conn, gid, tip, pid, market, line, edge, over=-110, under=-110, at=None):
    conn.execute(insert(pp).values(
        model="nbap15", model_version="nbap15:test", game_id=gid, player_id=pid,
        player_name=pid, market=market, prediction_point="pretip",
        predicted_at=at or tip - timedelta(hours=1), mean=10.0, line=line, over_price=over,
        under_price=under, p_over=0.5 + edge / 100, edge=edge))  # fmt: skip


def _close(conn, gid, pid, market, line, over, under, book):
    conn.execute(insert(prop_lines).values(
        source="espn_props", source_game_id=str(gid), game_id=gid, player_id=pid,
        market=market, book_id=book, kind="close", line=line, over_price=over,
        under_price=under, fetched_at=NOW))  # fmt: skip


def test_outcomes_bets_void_and_clv(setup, engine):
    gid, tip, box, book = setup
    pids = sorted(box)
    a, b, c, d = pids[:4]
    pts = {p: box[p].points for p in pids}
    with engine.begin() as conn:
        conn.execute(update(nba_player_games).where(
            (nba_player_games.c.game_id == gid) & (nba_player_games.c.player_id == d))
            .values(minutes=0.0, did_not_play=True))  # fmt: skip
        _pred(conn, gid, tip, a, "points", pts[a] - 0.5, edge=5.0)  # over bet, wins
        _pred(conn, gid, tip, b, "points", pts[b] + 0.5, edge=5.0)  # over bet, loses
        _pred(conn, gid, tip, c, "points", float(pts[c]), edge=-4.0)  # under bet, push
        _pred(conn, gid, tip, d, "points", 12.5, edge=6.0)  # inactive: void
        _pred(conn, gid, tip, a, "rebounds", 3.5, edge=1.0)  # below threshold: no bet
        _pred(conn, gid, tip, a, "assists", 2.5, edge=9.0)  # no validated threshold: no bet
        # A later prediction after tip is ignored; an earlier one is superseded.
        _pred(conn, gid, tip, b, "points", 99.5, edge=-9.0, at=tip + timedelta(minutes=5))
        _close(conn, gid, a, "points", pts[a] + 0.5, -110, -110, book)  # line moved up 1
        _close(conn, gid, c, "points", float(pts[c]), -130, +110, book)  # same line, price
        run = G.grade_nba_props(conn, NOW)
        again = G.grade_nba_props(conn, NOW)
        rows = {(r.player_id, r.market): r for r in conn.execute(select(prop_grades))}
        recs = {r.market: r for r in records(conn, ("nbap15",))}
    assert run.inserted == 6 and again.inserted == 0 and again.updated == 0
    assert rows[(a, "points")].outcome == "over" and rows[(a, "points")].profit == pytest.approx(
        100 / 110, abs=1e-4)
    assert rows[(a, "points")].clv == pytest.approx(1.0)  # close moved toward the over
    assert rows[(b, "points")].outcome == "under" and rows[(b, "points")].profit == -1.0
    assert rows[(b, "points")].clv is None and rows[(b, "points")].close_line is None
    assert rows[(c, "points")].outcome == "push" and rows[(c, "points")].profit == 0.0
    # Same line, under went from -110 to +110: its no-vig chance fell, so negative CLV.
    assert rows[(c, "points")].clv < 0
    assert rows[(d, "points")].outcome == "void" and rows[(d, "points")].actual is None
    assert rows[(a, "rebounds")].bet_side is None and rows[(a, "assists")].bet_side is None
    assert recs["points"].bets == 3 and (recs["points"].wins, recs["points"].losses) == (1, 1)
    assert recs["points"].void == 1


def test_pending_until_box_score(setup, engine):
    gid, tip, box, _ = setup
    with engine.begin() as conn:
        conn.execute(nba_player_games.delete().where(nba_player_games.c.game_id == gid))
        _pred(conn, gid, tip, "x1", "points", 10.5, edge=5.0)
        run = G.grade_nba_props(conn, NOW)
    assert run.pending == 1 and run.inserted == 0
