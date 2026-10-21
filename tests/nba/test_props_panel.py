"""M15 NBA prop inputs on the synthetic league: the as-of rule (a result or report after a
prediction point never changes that prediction's inputs) and usage redistribution when a star
sits (teammates' projected share rises; the team's expected total stays consistent)."""

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import insert, select, update

from betdata.db.schema import nba_injury_reports, nba_player_games, teams
from betdata.nba.props.panel import Panel, point_times
from tests.nba.synth import TEAMS, synth

PLAY = {"Out": 0.0, "Questionable": 0.5, "Probable": 0.9, "none|rep|0": 0.95,
        "none|norep|0": 0.95}  # fmt: skip


def _side(panel):
    g = panel.g
    pts = point_times(g, g.index)
    return pts.assign(pred_margin=2.0, pred_total=220.0)


def _features(engine, gid, points=("morning", "pretip")):
    with engine.connect() as conn:
        panel = Panel.load(conn)
    tg = panel.played_targets()
    tg = tg[tg.game_id == gid]
    tg = tg[tg.point.isin(points)]
    return panel.features(tg, _side(panel), PLAY).set_index(["player_id", "point"]).sort_index()


@pytest.fixture
def league(engine):
    return synth(engine)


def test_later_results_and_reports_never_change_inputs(league, engine):
    gid = league[25]
    before = _features(engine, gid)
    with engine.begin() as conn:
        tip = conn.execute(select(nba_player_games.c.known_at).where(
            nba_player_games.c.game_id == gid)).scalar() - timedelta(hours=3)  # fmt: skip
        # Results of this game and later ones change; a report row arrives after the points.
        conn.execute(update(nba_player_games).where(nba_player_games.c.game_id >= gid)
                     .values(minutes=40.0, points=55.0))  # fmt: skip
        tid = conn.execute(select(teams.c.team_id).where(teams.c.canonical_name == TEAMS[0])
                           ).scalar()  # fmt: skip
        conn.execute(insert(nba_injury_reports).values(
            game_date="2021-11-14", matchup="X@Y", game_id=gid, team_id=tid,
            team_name=TEAMS[0], player_name="Tma, Plb", status="Out", reason="Late",
            report_at=tip + timedelta(minutes=5), known_at=tip + timedelta(minutes=10),
            source="nba_injury", fetched_at=tip))  # fmt: skip
    after = _features(engine, gid)
    cols = [c for c in before.columns if c not in ("minutes",)]
    pd.testing.assert_frame_equal(before[cols], after[cols])


def test_star_out_redistributes_usage(league, engine):
    gid = league[30]
    with engine.connect() as conn:
        panel = Panel.load(conn)
        home = panel.g.at[gid, "home"]
        k = [t for t in TEAMS if conn.execute(select(teams.c.team_id).where(
            teams.c.canonical_name == t)).scalar() == home][0]  # fmt: skip
    tg = panel.played_targets()
    tg = tg[(tg.game_id == gid) & (tg.team_id == home) & (tg.point == "pretip")]
    star = f"{TEAMS.index(k)}0"
    base = panel.features(tg, _side(panel), PLAY).set_index("player_id")
    t = tg.t.iloc[0]
    out_row = pd.DataFrame([{"game_id": gid, "team_id": home, "player_id": star,
                             "player_name": "x", "status": "Out",
                             "known_at": t - timedelta(hours=1)}])  # fmt: skip
    panel.inj = pd.concat([panel.inj[panel.inj.game_id != gid], out_row], ignore_index=True)
    out = panel.features(tg, _side(panel), PLAY).set_index("player_id")
    mates = [p for p in base.index if p != star]
    # Every teammate's missing share rises, and so does his redistributed expected points.
    assert (out.loc[mates, "mis_pts"] > base.loc[mates, "mis_pts"]).all()
    boost = lambda f: (f.g10_pts / (1 - f.mis_pts)).loc[mates]  # noqa: E731
    gained = float((boost(out) - boost(base)).sum())
    # Team totals stay consistent: what the teammates gain is about what the star took away
    # (his expected points times the change in his chance of sitting).
    star_pts = float(panel.states[panel.states.player_id == star].sort_values("tip")
                     .g10_pts.iloc[-1])  # fmt: skip
    lost = star_pts * (0.95 - 0.0)  # his play probability went from 0.95 to 0 (Out)
    assert gained == pytest.approx(lost, rel=0.35)
    assert np.isfinite(out[["mis_min", "opp_pts", "side_total"]].to_numpy()).all()
