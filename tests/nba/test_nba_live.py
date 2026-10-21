"""M14 live NBA predictions: the frozen fit predicting upcoming games at the point each has
reached, back-filling a missed point, storing change-only."""

from datetime import timedelta

from sqlalchemy import insert, select

from betdata.core.matching import sport_id
from betdata.db.schema import games, teams
from betdata.nba.data import load_store
from betdata.nba.features import GROUPS
from betdata.nba.points import morning_time
from tests.nba.synth import synth


def test_live_predict_points_and_backfill(engine, tmp_path, monkeypatch):
    """The live path: a frozen fit predicts upcoming games at the point each has reached,
    back-fills a point the runner missed, and stores change-only."""
    import json

    from betdata.nba import live
    from betdata.nba.data import outcomes
    from betdata.nba.pipeline import POINTS, build, columns, fit_point

    ids = synth(engine)
    with engine.connect() as conn:
        store = load_store(conn)
    rows = build(store, list(GROUPS.values()), ids)
    m, t = columns(rows, ["avail", "str", "rest"])
    out = outcomes(store)
    fits = {p: fit_point(rows, out, p, [2021], m, t, 10.0, 10.0) for p in POINTS}
    fit = tmp_path / "fit.json"
    fit.write_text(json.dumps({"fits": {p: {"margin": f.margin.to_dict(),
                                            "total": f.total.to_dict(), "sigma": f.sigma}
                                        for p, f in fits.items()}, "play_prob": {}}))
    design = live.Design(groups=["avail", "str", "rest"], lam_m=10, lam_t=10,
                         spread_threshold=3, ml_threshold=4, blend_weight=0.3, fit=str(fit))
    monkeypatch.setattr(live.Design, "load", classmethod(lambda cls, path=None: design))
    tip = store.games.tip.max() + timedelta(days=1)
    with engine.begin() as conn:
        sid = sport_id(conn, "nba")
        tid = dict(conn.execute(select(teams.c.canonical_name, teams.c.team_id)
                                .where(teams.c.sport_id == sid)).all())  # fmt: skip
        gid = conn.execute(insert(games).values(
            sport_id=sid, season=2021, season_type="REG", start_time_utc=tip,
            home_team_id=tid["Boston Celtics"], away_team_id=tid["Miami Heat"],
            status="scheduled")).inserted_primary_key[0]  # fmt: skip
    from betdata.nba.points import pretip_time

    early = tip - timedelta(hours=20)
    picks = live.run_predict(engine, early)
    assert [p.point for p in picks if p.game_id == gid] == ["early"]
    assert live.run_predict(engine, early + timedelta(hours=1)) and True
    late = pretip_time(tip) + timedelta(minutes=1)  # the morning run was missed
    live.run_predict(engine, late)
    from betdata.db.schema import model_predictions as mp

    with engine.connect() as conn:
        rows = conn.execute(select(mp.c.prediction_point, mp.c.predicted_at)
                            .where(mp.c.game_id == gid)).all()  # fmt: skip
    points = [r.prediction_point for r in rows]
    assert points.count("early") == 1  # unchanged an hour later: not stored again
    assert "morning" in points and "pretip" in points
    back = next(r for r in rows if r.prediction_point == "morning")
    assert back.predicted_at == morning_time(tip)  # as of the missed point
