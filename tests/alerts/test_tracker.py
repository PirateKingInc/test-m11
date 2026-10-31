"""Today's tracker (alerts/tracker.py, MODEL.md "Today's tracker"): today's picks grouped by
game with status and score; price status before the game; the result in plain words at the
final; live scores from balldontlie when a key is set; pushed to the `tracker` branch."""

import json
import subprocess
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, insert

from betdata.alerts import tracker as T
from betdata.alerts.db import alert_grades, alerts, consensus_grades, engine_for, quotes, set_state
from betdata.db.schema import games, metadata, nba_player_games, results, sports

DAY = date(2026, 10, 31)
NOW = datetime(2026, 11, 1, 1, 0, tzinfo=UTC)  # Sat 9:00 PM ET
PRE = NOW + timedelta(hours=1)  # tips at 10 PM ET
LIVE = NOW - timedelta(minutes=50)
FINAL = NOW - timedelta(hours=3)


@pytest.fixture
def dbs(tmp_path):
    pdb = engine_for(tmp_path / "poller.sqlite")
    main = create_engine(f"sqlite:///{tmp_path / 'main.sqlite'}")
    metadata.create_all(main)
    return pdb, main


def _alert(p, key, gid, label, tip, market="assists", player="Anthony Black", side="under",
           line=3.5, price=-110, details=None):  # fmt: skip
    return p.execute(insert(alerts).values(
        signal="snipe", sport="nba", game_id=gid, game_label=label, tip=tip, market=market,
        player_name=player, side=side, line=line, price=price, book="fanduel", fair_prob=0.6,
        book_prob=0.5, reason="r", dedup_key=key, created_at=NOW - timedelta(hours=8),
        quiet=False, sent=True, details=details or {"edge": 0.1})).inserted_primary_key[0]


def _fixture(pdb, main):
    with pdb.begin() as p:
        _alert(p, "pre", 1, "NYK @ BOS", PRE, player="Jalen Brunson", side="over", line=5.5,
               price=120)  # fmt: skip
        p.execute(insert(quotes).values(polled_at=NOW - timedelta(minutes=3), sport="nba",
                                        game_id=1, source="sharpapi", book="fanduel",
                                        market="assists", player_name="Jalen Brunson",
                                        outcome="over", line=5.5, price=120))  # fmt: skip
        _alert(p, "pre2", 1, "NYK @ BOS", PRE, player="Josh Hart", side="over", line=8.5)
        _alert(p, "live", 2, "LAL @ GSW", LIVE, player="Austin Reaves")
        won = _alert(p, "won", 3, "ORL @ MIA", FINAL, details={"edge": 0.1, "label": {
            "label": "TAKE", "stake": 1.0}})
        out = _alert(p, "out", 3, "ORL @ MIA", FINAL, player="Jalen Suggs", market="points",
                     side="over", line=14.5)  # fmt: skip
        tot = _alert(p, "tot", 3, "ORL @ MIA", FINAL, market="totals", player=None,
                     side="over", line=210.5)  # fmt: skip
        for aid, res, profit in ((won, "win", 0.909), (out, "void", 0.0), (tot, "loss", -1.0)):
            p.execute(insert(alert_grades).values(alert_id=aid, result=res, profit=profit,
                                                  graded_at=NOW))  # fmt: skip
        p.execute(insert(consensus_grades).values(alert_id=won, clv=0.021, graded_at=NOW))
        set_state(p, "card:nba:2026-10-31", {"sport": "nba", "day": "2026-10-31", "picks": [
            {"dedup_key": "pre", "label": "LEAN", "stake": 0.5},
            {"dedup_key": "won", "label": "LEAN", "stake": 0.5}]}, NOW)
    with main.begin() as m:
        m.execute(insert(results).values(game_id=3, source="espn", home_score=101,
                                         away_score=104, final=True))  # fmt: skip
        for name, dnp, mins, ast, pts in (("Anthony Black", False, 30.0, 2.0, 11.0),
                                          ("Jalen Suggs", True, 0.0, 0.0, 0.0)):
            m.execute(insert(nba_player_games).values(
                game_id=3, team_id=1, player_id=name, player_name=name, did_not_play=dnp,
                minutes=mins, ast=ast, points=pts, source="espn", known_at=NOW,
                fetched_at=NOW))  # fmt: skip


def test_the_document_has_each_state(dbs):
    pdb, main = dbs
    _fixture(pdb, main)
    with pdb.connect() as p, main.connect() as m:
        doc = T.build(p, m, "nba", DAY, NOW)
    g = {x["label"]: x for x in doc["games"]}
    assert [x["label"] for x in doc["games"]] == ["ORL @ MIA", "LAL @ GSW", "NYK @ BOS"]
    pre, live, fin = g["NYK @ BOS"], g["LAL @ GSW"], g["ORL @ MIA"]
    assert pre["state"] == "pre" and live["state"] == "live" and fin["state"] == "final"
    assert fin["score"] == {"home": 101, "away": 104}
    # pre-game: the card pick with the card's label and stake, its price status vs the latest poll
    brunson = pre["picks"][0]
    assert (brunson["card"], brunson["label"], brunson["stake"]) == (True, "LEAN", 0.5)
    assert (brunson["pick"], brunson["odds"], brunson["book"], brunson["price"]) == (
        "Jalen Brunson over 5.5 assists", "+120", "FanDuel", "Live")
    assert pre["picks"][1]["card"] is False and pre["picks"][1]["stake"] == 1.0
    # final: plain words with the actual stat, units at the pick's stake, CLV
    won, out, tot = fin["picks"]
    assert won["text"] == "Anthony Black: 2 assists — under 3.5 WIN, +0.45u"
    assert won["units"] == 0.45 and won["clv"] == 2.1 and won["label"] == "LEAN"  # the card's
    assert out["text"] == "VOID (did not play)"
    assert tot["text"] == "Total 205 — over 210.5 LOSS, -1.00u"
    assert json.loads(json.dumps(doc)) == doc  # plain JSON


def test_live_scores_from_balldontlie_when_a_key_is_set(dbs):
    pdb, main = dbs
    _fixture(pdb, main)
    seen = []

    def answer(r):
        seen.append((str(r.url), r.headers.get("authorization")))
        return httpx.Response(200, json={"data": [
            {"status": "3rd Qtr", "period": 3, "time": "4:12", "home_team_score": 77,
             "visitor_team_score": 70, "home_team": {"abbreviation": "GSW"},
             "visitor_team": {"abbreviation": "LAL"}},
            {"status": "2026-11-01T02:00:00Z", "period": 0, "time": "",
             "home_team_score": 0, "visitor_team_score": 0,
             "home_team": {"abbreviation": "BOS"}, "visitor_team": {"abbreviation": "NY"}}]})

    client = httpx.Client(transport=httpx.MockTransport(answer))
    sc = T.bdl_scores("nba", DAY, "k-1", client)
    assert seen[0][1] == "k-1" and "dates%5B%5D=2026-10-31" in seen[0][0]
    assert sc[("LAL", "GSW")] == {"state": "live", "home": 77, "away": 70, "period": "3rd Qtr",
                                  "clock": "4:12"}  # fmt: skip
    assert sc[("NYK", "BOS")]["state"] == "pre"  # "NY" is our NYK
    with pdb.connect() as p, main.connect() as m:
        doc = T.build(p, m, "nba", DAY, NOW, sc)
    live = next(g for g in doc["games"] if g["label"] == "LAL @ GSW")
    assert live["score"] == {"home": 77, "away": 70} and live["period"] == "3rd Qtr"
    assert doc["scores"] == "balldontlie"
    assert T.bdl_scores("nba", DAY, None) == {}  # no key: no scores, no request


def test_the_window_runs_from_30_minutes_before_to_4_hours_after(dbs):
    _, main = dbs
    with main.begin() as m:
        m.execute(insert(sports).values(sport_id=2, key="nba", name="NBA"))
        m.execute(insert(games).values(game_id=9, sport_id=2, season=2027, home_team_id=1,
                                       away_team_id=2, start_time_utc=PRE, status="scheduled"))
    assert not T.in_window(main, PRE - timedelta(minutes=31))
    assert T.in_window(main, PRE - timedelta(minutes=30))
    assert T.in_window(main, PRE + timedelta(hours=4))
    assert not T.in_window(main, PRE + timedelta(hours=4, minutes=1))


def test_publish_force_pushes_one_commit_to_the_tracker_branch(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    assert T.publish({"generated_at": "a", "n": 1}, "o/r", None) == "no GITHUB_TOKEN"
    for n in (1, 2):
        assert T.publish({"generated_at": str(n), "n": n}, "o/r", "tok", str(bare)) == "pushed"
    log = subprocess.run(["git", "--git-dir", str(bare), "log", "--format=%s", "tracker"],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    assert log[0] == "tracker 2" and len([x for x in log if x]) == 1  # one commit, replaced
    body = subprocess.run(["git", "--git-dir", str(bare), "show", "tracker:tracker.json"],
                          capture_output=True, text=True, check=True).stdout
    assert json.loads(body) == {"generated_at": "2", "n": 2}
