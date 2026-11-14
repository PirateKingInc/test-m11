"""Both sides of h2h / spreads at the morning and pre-tip snapshots (owner 2026-11-14,
nba/engine_game_grades.py)."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select

from betdata.alerts.db import engine_for, quotes
from betdata.core.matching import sport_id
from betdata.db.schema import games, results, teams
from betdata.ledger import read_rows
from betdata.nba import engine_game_grades as GG

TIP = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)  # 7 PM ET Nov 15
MORNING = datetime(2026, 11, 15, 11, 15, tzinfo=UTC)  # 6:15 AM ET
NOW = TIP + timedelta(hours=10)


def _snap(gid, at, p_home=0.7, home_by=8):
    margin = [0.0] * 141
    margin[70 + home_by - 2] = 0.5  # home by home_by - 2 or home_by + 2
    margin[70 + home_by + 2] = 0.5
    return {"game_id": gid, "label": "NYK @ BOS", "tip": TIP.isoformat(), "p_home_win": p_home,
            "margin": {"lo": -70, "p": margin}, "total": {"lo": 140, "p": [1 / 181] * 181},
            "players": [], "snapshot_at": at.isoformat(), "version": "nbae20"}


def test_morning_snapshot_only_in_the_6am_hour_and_once(tmp_path):
    d = tmp_path / "m"
    doc = {"generated_at": MORNING.isoformat(), "version": "v", "games": [_snap(1, MORNING)]}
    assert GG.morning_snapshot(doc, MORNING - timedelta(hours=2), d) == 0  # 4:15 AM ET
    assert GG.morning_snapshot(doc, MORNING, d) == 1
    later = {**doc, "generated_at": (MORNING + timedelta(minutes=30)).isoformat()}
    assert GG.morning_snapshot(later, MORNING + timedelta(minutes=30), d) == 0  # write-once
    assert json.loads((d / "1.json").read_text())["snapshot_at"] == MORNING.isoformat()


def test_both_sides_at_both_snapshots(engine, tmp_path):
    with engine.begin() as c:
        sid = sport_id(c, "nba")
        ids = dict(c.execute(select(teams.c.abbrev, teams.c.team_id)
                             .where(teams.c.sport_id == sid)).all())  # fmt: skip
        gid = c.execute(insert(games).values(
            sport_id=sid, season=2026, season_type="REG", start_time_utc=TIP,
            home_team_id=ids["BOS"], away_team_id=ids["NYK"],
            status="final")).inserted_primary_key[0]  # fmt: skip
        c.execute(insert(results).values(game_id=gid, source="espn", home_score=110,
                                         away_score=104, final=True))  # home by 6
    pdb = engine_for(tmp_path / "poller.sqlite")
    with pdb.begin() as p:
        for at, line, hp, ap, mh, ma in (  # the morning line, then the line moves
                (MORNING - timedelta(minutes=30), -5.5, -110, -110, -240, 200),
                (TIP - timedelta(minutes=40), -7.5, -105, -115, -300, 250)):
            for side, ln, pr in (("home", line, hp), ("away", -line, ap)):
                p.execute(insert(quotes).values(
                    polled_at=at, sport="nba", game_id=gid, source="sharpapi",
                    book="draftkings", market="spreads", player_name=None, outcome=side,
                    line=ln, price=pr))  # fmt: skip
            for side, pr in (("home", mh), ("away", ma)):
                p.execute(insert(quotes).values(
                    polled_at=at, sport="nba", game_id=gid, source="sharpapi",
                    book="draftkings", market="h2h", player_name=None, outcome=side,
                    line=None, price=pr))  # fmt: skip
    md, pd_ = tmp_path / "m", tmp_path / "p"
    md.mkdir()
    pd_.mkdir()
    (md / f"{gid}.json").write_text(json.dumps(_snap(gid, MORNING)))
    (pd_ / f"{gid}.json").write_text(json.dumps(_snap(gid, TIP - timedelta(minutes=30), 0.75)))
    out = tmp_path / "ledger"
    out.mkdir()
    with pdb.connect() as p, engine.connect() as m:
        assert GG.grade(p, m, out, NOW, md, pd_) == 8  # 2 markets x 2 sides x 2 snapshots
        assert GG.grade(p, m, out, NOW, md, pd_) == 0  # once
    rows = {(r["market"], r["side"], r["snapshot"]): r for r in read_rows(out / GG.FILE)}
    sp_m_h, sp_m_a = rows[("spreads", "home", "morning")], rows[("spreads", "away", "morning")]
    assert float(sp_m_h["line"]) == -5.5 and float(sp_m_a["line"]) == 5.5  # the morning line
    assert float(rows[("spreads", "home", "pretip")]["line"]) == -7.5  # the pre-tip line
    # home by 6 or 10 (50/50): covers -5.5 always; at -7.5, half the time
    assert float(sp_m_h["p_engine"]) == 1.0 and float(sp_m_a["p_engine"]) == 0.0
    assert sp_m_h["result"] == "win" and sp_m_a["result"] == "loss"  # home won by 6
    pt_h, pt_a = rows[("spreads", "home", "pretip")], rows[("spreads", "away", "pretip")]
    assert float(pt_h["p_engine"]) == 0.5 and pt_h["result"] == "loss" and pt_a["result"] == "win"
    for k in ("morning", "pretip"):  # away = 1 - home where it should
        for mk in ("spreads", "h2h"):
            h, a = rows[(mk, "home", k)], rows[(mk, "away", k)]
            assert abs(float(h["p_engine"]) + float(a["p_engine"]) - 1) < 1e-9
            assert abs(float(h["p_novig"]) + float(a["p_novig"]) - 1) < 1e-9
            assert (h["price"], h["other_price"]) == (a["other_price"], a["price"])
    assert float(rows[("h2h", "home", "morning")]["p_engine"]) == 0.7
    assert float(rows[("h2h", "home", "pretip")]["p_engine"]) == 0.75
    assert rows[("h2h", "away", "pretip")]["result"] == "loss"
    assert rows[("spreads", "home", "pretip")]["price"] == "-105"
