"""Every NBA engine prediction graded at the close (owner override 2026-11-04,
nba/engine_grades.py): a snapshot of the last pre-tip engine document per game; once final, one
row per game market and projected prop with a stored DK/FD closing line (engine probability at
the closing line, closing no-vig, result), appended once; calibration and Brier vs the close."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select

from betdata.alerts.db import engine_for, quotes
from betdata.core.matching import sport_id
from betdata.db.schema import games, nba_player_games, results, teams
from betdata.ledger import read_rows
from betdata.nba import engine_grades as G

TIP = datetime(2026, 11, 6, 0, 0, tzinfo=UTC)
NOW = TIP + timedelta(hours=10)


def _snap(gid):
    margin = [0.0] * 141
    margin[70 + 8] = 1.0  # home by 8, always
    cdf = [0.0] * 20 + [1.0] * 30  # points: always exactly 20
    return {"game_id": gid, "label": "NYK @ BOS", "tip": TIP.isoformat(), "p_home_win": 0.9,
            "margin": {"lo": -70, "p": margin}, "total": {"lo": 140, "p": [1 / 181] * 181},
            "players": [{"name": "Jayson Tatum", "team_id": 1, "cdf": {"pts": cdf}},
                        {"name": "No Line", "team_id": 1, "cdf": {"pts": cdf}}],
            "snapshot_at": (TIP - timedelta(minutes=50)).isoformat(), "version": "m20"}


def test_snapshot_keeps_upcoming_games_only(tmp_path):
    d = tmp_path / "snaps"
    doc = {"generated_at": (TIP - timedelta(hours=1)).isoformat(), "version": "m20",
           "games": [_snap(1), {**_snap(2), "tip": (TIP - timedelta(hours=2)).isoformat()}]}
    assert G.snapshot(doc, TIP - timedelta(hours=1), d) == 1
    assert [f.name for f in d.glob("*.json")] == ["1.json"]
    assert json.loads((d / "1.json").read_text())["snapshot_at"] == doc["generated_at"]
    G.snapshot({"games": []}, TIP + timedelta(days=8), d)  # pruned a week after the tip
    assert list(d.glob("*.json")) == []


def test_final_games_are_graded_once_against_the_close(engine, tmp_path):
    with engine.begin() as c:
        sid = sport_id(c, "nba")
        ids = dict(c.execute(select(teams.c.abbrev, teams.c.team_id)
                             .where(teams.c.sport_id == sid)).all())  # fmt: skip
        gid = c.execute(insert(games).values(
            sport_id=sid, season=2026, season_type="REG", start_time_utc=TIP,
            home_team_id=ids["BOS"], away_team_id=ids["NYK"],
            status="final")).inserted_primary_key[0]  # fmt: skip
        c.execute(insert(results).values(game_id=gid, source="espn", home_score=110,
                                         away_score=100, final=True))
    pdb = engine_for(tmp_path / "poller.sqlite")
    at = TIP - timedelta(minutes=10)
    with pdb.begin() as p:
        for book, market, player, side, line, price in (
                ("draftkings", "spreads", None, "home", -6.5, -110),
                ("draftkings", "spreads", None, "away", 6.5, -110),
                ("fanduel", "totals", None, "over", 221.5, -105),  # DK has no total: FD's
                ("fanduel", "totals", None, "under", 221.5, -115),
                ("draftkings", "points", "Jayson Tatum", "over", 22.5, -120),
                ("draftkings", "points", "Jayson Tatum", "under", 22.5, 100)):
            p.execute(insert(quotes).values(
                polled_at=at, sport="nba", game_id=gid, source="sharpapi", book=book,
                market=market, player_name=player, outcome=side, line=line, price=price))
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    (snaps / f"{gid}.json").write_text(json.dumps({**_snap(gid), "game_id": gid}))
    out = tmp_path / "ledger"
    out.mkdir()
    with pdb.connect() as p, engine.connect() as m:  # no box score yet: nothing graded
        assert G.grade_games(p, m, out, NOW, snaps) == 0
    with engine.begin() as c:
        c.execute(insert(nba_player_games).values(
            game_id=gid, team_id=ids["BOS"], player_id="9", player_name="Jayson Tatum",
            minutes=35.0, points=19.0, source="espn", known_at=NOW, fetched_at=NOW))
    with pdb.connect() as p, engine.connect() as m:
        assert G.grade_games(p, m, out, NOW, snaps) == 3
        assert G.grade_games(p, m, out, NOW + timedelta(hours=1), snaps) == 0  # once
    rows = {r["market"]: r for r in read_rows(out / G.FILE)}
    assert set(rows) == {"spreads", "totals", "points"}  # no line, no row (h2h, "No Line")
    sp = rows["spreads"]
    assert (sp["side"], float(sp["line"]), sp["book"], sp["result"], sp["y"]) == (
        "home", -6.5, "draftkings", "win", "1")
    assert float(sp["p_engine"]) == 1.0 and float(sp["p_close"]) == 0.5
    assert rows["totals"]["book"] == "fanduel" and rows["totals"]["result"] == "loss"
    pts = rows["points"]
    assert (pts["player"], pts["result"], float(pts["p_engine"])) == ("Jayson Tatum", "loss",
                                                                     0.0)
    s = G.summary(read_rows(out / G.FILE))
    assert s.n == 3 and s.brier_engine is not None and s.brier_close is not None
    assert "3 graded · Brier engine" in G.summary_line(s)
    assert G.summary(read_rows(out / G.FILE), "prop").n == 1


def test_summary_line_with_no_rows():
    assert G.summary_line(G.summary([])).startswith("No graded predictions yet")
