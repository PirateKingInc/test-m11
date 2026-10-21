"""M15 NFL props live and grading: ESPN id -> gsis id mapping, change-only storage of live
predictions, and grading from nflverse weekly stats (a stat line, played without one = 0, did
not play = void, stats not in yet = pending)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import insert, select, update

from betdata.db.schema import (
    game_source_ids,
    games,
    player_game_snaps,
    prop_grades,
    prop_predictions,
    roster_weeks,
)
from betdata.nfl.props import grade as G
from betdata.nfl.props import live as L
from betdata.nfl.props.ids import espn_to_gsis, norm_name
from betdata.sources.nflverse import NflverseSource
from tests.conftest import fixture_bytes

NID = "2025_01_DAL_PHI"


@pytest.fixture
def db(engine, settings, fake_api, clock, tmp_path, monkeypatch):
    nfl = NflverseSource(engine, settings.source("nflverse"), transport=fake_api.transport,
                         sleep=lambda s: None, clock=clock)  # fmt: skip
    fake_api.add(body=fixture_bytes("nflverse", "games_sample.csv"))
    assert nfl.run("schedules", seasons="all").status == "ok"
    monkeypatch.setenv("BETDATA_DATA_DIR", str(tmp_path))
    root = tmp_path / "nflverse"
    (root / "depth_charts").mkdir(parents=True)
    pd.DataFrame({"dt": ["2025-09-01T00:00:00Z"] * 3, "espn_id": ["101", "102", "103"],
                  "gsis_id": ["00-1", "00-2", "00-3"]}).to_parquet(
        root / "depth_charts" / "depth_charts_2025.parquet")  # fmt: skip
    (root / "stats_player").mkdir()
    pd.DataFrame({"player_id": ["00-1", "00-9"], "player_display_name": ["Al One", "Zed"],
                  "game_id": [NID, NID], "passing_yards": [0, 250], "rushing_yards": [12, 0],
                  "receiving_yards": [87, 0], "receptions": [6, 0], "rushing_tds": [0, 0],
                  "receiving_tds": [1, 0]}).to_parquet(
        root / "stats_player" / "stats_player_week_2025.parquet")  # fmt: skip
    with engine.begin() as conn:
        gid = conn.execute(select(game_source_ids.c.game_id).where(
            game_source_ids.c.source_game_id == NID)).scalar()  # fmt: skip
        g = conn.execute(select(games).where(games.c.game_id == gid)).first()
        conn.execute(update(games).where(games.c.game_id == gid).values(status="final"))
        for pfr, gsis, snaps in (("B00", "00-2", 30), ("C00", "00-3", 0)):
            conn.execute(insert(player_game_snaps).values(
                source="nflverse", source_game_id=NID, game_id=gid, season=2025, week=1,
                team_id=g.home_team_id, player_name=pfr, pfr_player_id=pfr, position="WR",
                offense_snaps=snaps, offense_pct=snaps / 60))  # fmt: skip
            conn.execute(insert(roster_weeks).values(
                source="nflverse", season=2025, week=1, team_id=g.home_team_id, gsis_id=gsis,
                pfr_id=pfr, player_name=pfr, status="ACT"))  # fmt: skip
    return gid, g.start_time_utc


def _pred(conn, gid, tip, pid, market, line, mean=50.0, at=None):
    conn.execute(insert(prop_predictions).values(
        model="nflp15", model_version="nflp15:test", game_id=gid, player_id=pid,
        player_name=f"Player {pid}", market=market, prediction_point="prekick",
        predicted_at=at or tip - timedelta(hours=1), mean=mean, line=line, over_price=-110,
        under_price=-110, p_over=0.55, edge=2.0))  # fmt: skip


def test_espn_ids_map_by_depth_chart_then_name(tmp_path):
    root = tmp_path / "nflverse" / "depth_charts"
    root.mkdir(parents=True)
    pd.DataFrame({"dt": ["2026-09-01T00:00:00Z"], "espn_id": ["4241389"],
                  "gsis_id": ["00-0036900"]}).to_parquet(root / "depth_charts_2026.parquet")
    lines = pd.DataFrame({"player_id": ["4241389", "999", "998"],
                          "player_name": ["Ja'Marr Chase", "Kenneth Walker III", "Nobody"]})
    stats = pd.DataFrame({"player_id": ["00-0037", "00-0038"],
                          "name": ["Kenneth Walker", "Someone Else"]})
    got = espn_to_gsis(lines, tmp_path, (2025, 2026), stats)
    assert got.tolist() == ["00-0036900", "00-0037", None]
    assert norm_name("Ja'Marr Chase Jr.") == "jamarr chase"


def test_grading_actuals_and_void(db, engine):
    gid, tip = db
    with engine.begin() as conn:
        _pred(conn, gid, tip, "101", "rec_yds", 70.5)  # 87 -> over
        _pred(conn, gid, tip, "101", "anytime_td", None)  # 1 TD -> yes (over)
        _pred(conn, gid, tip, "102", "receptions", 3.5)  # played, no stat line -> 0, under
        _pred(conn, gid, tip, "103", "rec_yds", 40.5)  # no snaps -> void
        got = G.actuals(conn, [(gid, "101", "rec_yds"), (gid, "102", "receptions"),
                               (gid, "103", "rec_yds"), (gid + 1, "101", "rec_yds")])
        assert got == {(gid, "101", "rec_yds"): 87.0, (gid, "102", "receptions"): 0.0,
                       (gid, "103", "rec_yds"): G.VOID}  # fmt: skip
        run = G.grade_nfl_props(conn, datetime(2025, 9, 10, tzinfo=UTC))
        rows = {(r.player_id, r.market): r for r in conn.execute(select(prop_grades))}
    assert run.inserted == 4
    assert rows[("101", "rec_yds")].outcome == "over"
    assert rows[("101", "anytime_td")].outcome == "over"
    assert rows[("102", "receptions")].outcome == "under"
    assert rows[("103", "rec_yds")].outcome == "void"
    assert all(r.bet_side is None for r in rows.values())  # research-only: no bets


def test_live_storage_is_change_only(db, engine, tmp_path, monkeypatch):
    gid, tip = db
    design = tmp_path / "nfl_props.json"
    design.write_text("{}")
    monkeypatch.setattr(L, "DESIGN_PATH", design)
    pick = dict(game_id=gid, player_id="101", player_name="Al One", team_id=None,
                market="rec_yds", prediction_point="final_report", mean=61.2, line=55.5,
                over_price=-115, under_price=-105, book_id=1, line_fetched_at=tip,
                p_over=0.56, edge=3.1, dist={"family": "negbin"},
                factors=[["wind 25 km/h", -4.2, "yds"], ["Kelce out", 3.0, "yds"]],
                model="nflp15", model_version="nflp15:abc")  # fmt: skip
    picks = [pick]
    monkeypatch.setattr(L, "predict", lambda conn, now: picks)
    now = tip - timedelta(days=2)
    assert L.run(engine, now) == 1
    assert L.run(engine, now + timedelta(hours=1)) == 0  # unchanged: nothing stored
    picks[0] = {**pick, "mean": 64.0}
    assert L.run(engine, now + timedelta(hours=2)) == 1
    with engine.connect() as conn:
        rows = conn.execute(select(prop_predictions)).mappings().all()
    assert [r["mean"] for r in rows] == [61.2, 64.0]
    assert rows[0]["factors"][0] == ["wind 25 km/h", -4.2, "yds"]
