"""M15 live NBA prop predictions on the synthetic league: only players with a stored line are
predicted, at the game's current point, with P(over) and an edge against the no-vig line, the
top two factors, and change-only storage."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import insert, select, update

from betdata.db.schema import books, games, prop_lines, prop_predictions
from betdata.nba.props import live as L
from betdata.nba.props import model as M
from betdata.nba.props.panel import Panel
from tests.nba.synth import synth


@pytest.fixture
def setup(engine, tmp_path, monkeypatch):
    ids = synth(engine, n_days=30)
    gid = ids[-1]
    with engine.begin() as conn:
        tip = conn.execute(select(games.c.start_time_utc).where(games.c.game_id == gid)).scalar()
        conn.execute(update(games).where(games.c.game_id == gid).values(status="scheduled"))
        from betdata.db.schema import nba_player_games
        conn.execute(nba_player_games.delete().where(nba_player_games.c.game_id == gid))
        home = conn.execute(select(games.c.home_team_id).where(games.c.game_id == gid)).scalar()
        panel = Panel.load(conn)
    # A small frozen fit from the synthetic history.
    tg = panel.played_targets()
    side = tg[["game_id", "point"]].drop_duplicates().assign(pred_margin=0.0, pred_total=220.0)
    f = M.prepare(panel.features(tg, side, {}))
    y = panel.p.set_index(["game_id", "player_id"])[["minutes", "pts", "reb", "ast", "fg3m",
                                                     "pra"]]  # fmt: skip
    f = f[f.has_state].join(y, on=["game_id", "player_id"])
    fit = tmp_path / "fit.json"
    fit.write_text(json.dumps({"model": M.fit(f).to_dict()}))
    design = L.PropDesign(fit=str(fit), thresholds={"points": 3.0},
                          test_roi={"points": -0.02}, research_only=["pra"])  # fmt: skip
    monkeypatch.setattr(L.PropDesign, "load", classmethod(lambda cls: design))
    monkeypatch.setattr(L, "DESIGN_PATH", fit)
    return gid, tip, home


def _line(conn, gid, pid, market, line, over, under, at):
    book = conn.execute(select(books.c.book_id).where(books.c.key == "draftkings")).scalar()
    conn.execute(insert(prop_lines).values(
        source="espn_props", source_game_id=str(gid), game_id=gid, player_id=pid,
        player_name=f"P {pid}", market=market, book_id=book, kind="poll", line=line,
        over_price=over, under_price=under, fetched_at=at))  # fmt: skip


def test_predicts_players_with_lines_change_only(setup, engine):
    gid, tip, home = setup
    now = tip - timedelta(hours=3)
    with engine.begin() as conn:
        team = conn.execute(select(games.c.home_team_id).where(games.c.game_id == gid)).scalar()
        from betdata.db.schema import teams
        from tests.nba.synth import TEAMS
        name = conn.execute(select(teams.c.canonical_name).where(teams.c.team_id == team)
                            ).scalar()  # fmt: skip
        k = TEAMS.index(name)
        _line(conn, gid, f"{k}1", "points", 15.5, -115, -105, now - timedelta(hours=1))
        _line(conn, gid, f"{k}2", "rebounds", 3.5, +100, -120, now - timedelta(hours=1))
        _line(conn, gid, f"{k}3", "points", 15.5, -110, -110, now + timedelta(hours=1))  # later
    assert L.run(engine, now) == 2
    assert L.run(engine, now + timedelta(minutes=1)) == 0  # nothing changed
    with engine.connect() as conn:
        rows = conn.execute(select(prop_predictions)).mappings().all()
    by = {r["player_id"]: r for r in rows}
    assert set(by) == {f"{k}1", f"{k}2"}
    p = by[f"{k}1"]
    assert p["model"] == "nbap15" and p["prediction_point"] == "morning"
    assert 0 < p["p_over"] < 1 and p["edge"] is not None and p["line"] == 15.5
    assert p["dist"]["family"] == "negbin" and p["dist"]["minutes"] > 0
    assert 1 <= len(p["factors"]) <= 2 and all(len(x) == 3 for x in p["factors"])


def test_flag_rule():
    d = L.PropDesign(fit="x", test_roi={"points": 0.03, "rebounds": -0.01, "pra": 0.2},
                     research_only=["pra"])  # fmt: skip
    assert d.flag_allowed("points") and not d.flag_allowed("rebounds")
    assert not d.flag_allowed("pra") and not d.flag_allowed("assists")
