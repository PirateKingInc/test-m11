"""M19 review fields and reason tags (betdata/review.py, MODEL.md "M19"): pick-time and result
fields written once to the ledger; one primary tag per graded pick, by the fixed rules."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, insert, update

from betdata import ledger as L
from betdata import official as O
from betdata import review as RV
from betdata.alerts.db import alert_grades, alerts, consensus_grades, engine_for
from betdata.db.schema import (
    games,
    metadata,
    nba_injury_reports,
    nba_player_games,
    results,
    sports,  # fmt: skip
    teams,
)

TIP = datetime(2026, 11, 1, 23, 30, tzinfo=UTC)
PICK = TIP - timedelta(hours=6)


@pytest.fixture
def dbs(tmp_path):
    pdb = engine_for(tmp_path / "poller.sqlite")
    main = create_engine(f"sqlite:///{tmp_path / 'main.sqlite'}")
    metadata.create_all(main)
    with main.begin() as m:
        m.execute(insert(sports).values(sport_id=2, key="nba", name="NBA"))
        m.execute(insert(teams), [dict(team_id=1, sport_id=2, canonical_name="Orlando",
                                       abbrev="ORL"),
                                  dict(team_id=2, sport_id=2, canonical_name="Miami",
                                       abbrev="MIA")])  # fmt: skip
        m.execute(insert(games).values(game_id=7, sport_id=2, season=2027, start_time_utc=TIP,
                                       home_team_id=2, away_team_id=1, status="final"))
        # 10 earlier games: Banchero 34 min (key), Bench 12 min (not key)
        for i in range(10):
            gid = 100 + i
            t = TIP - timedelta(days=2 * (i + 1))
            m.execute(insert(games).values(game_id=gid, sport_id=2, season=2027,
                                           start_time_utc=t, home_team_id=1, away_team_id=2,
                                           status="final"))  # fmt: skip
            for name, mins, tid in (("Paolo Banchero", 34.0, 1), ("Ben Bench", 12.0, 1),
                                    ("Anthony Black", 28.0, 1), ("Bam Adebayo", 33.0, 2)):
                m.execute(insert(nba_player_games).values(
                    game_id=gid, team_id=tid, player_id=name, player_name=name, minutes=mins,
                    source="espn", known_at=t, fetched_at=t))  # fmt: skip
        m.execute(insert(results).values(game_id=7, source="espn", home_score=101,
                                         away_score=104, final=True))  # fmt: skip
    return pdb, main


def _report(main, name, status, at):
    with main.begin() as m:
        m.execute(insert(nba_injury_reports).values(
            game_date="2026-11-01", matchup="ORL@MIA", game_id=7, team_id=1, team_name="Orlando",
            player_name=name, status=status, report_at=at, known_at=at, source="nba",
            fetched_at=at))  # fmt: skip


def _box(main, name, mins, ast, tid=1):
    with main.begin() as m:
        m.execute(insert(nba_player_games).values(
            game_id=7, team_id=tid, player_id=name, player_name=name, minutes=mins, ast=ast,
            source="espn", known_at=TIP, fetched_at=TIP))  # fmt: skip


def _pick(pdb, key, result="win", profit=0.9, clv=0.02, player="Anthony Black", market="assists",
          side="under", line=3.5, details=None):  # fmt: skip
    with pdb.begin() as p:
        aid = p.execute(insert(alerts).values(
            signal="snipe", sport="nba", game_id=7, game_label="ORL @ MIA", tip=TIP,
            market=market, player_name=player, side=side, line=line, price=-110,
            book="fanduel", fair_prob=0.6, book_prob=0.5, reason="r", dedup_key=key,
            created_at=PICK, quiet=False, sent=True,
            details=details if details is not None else {"edge": 0.1, "median": 2.0,
                                                         "minutes": 30.0,
                                                         "team_id": 1})).inserted_primary_key[0]
        if result:
            p.execute(insert(alert_grades).values(alert_id=aid, result=result, profit=profit,
                                                  close_line=line, graded_at=TIP))  # fmt: skip
            if clv is not None:
                p.execute(insert(consensus_grades).values(alert_id=aid, clv=clv, graded_at=TIP))
    return aid


def _sync(pdb, main, out, now=TIP + timedelta(hours=4)):
    with pdb.connect() as p, main.connect() as m:
        L.sync(p, m, out, now)


@pytest.mark.parametrize("kw,want", [
    (dict(news=True, proj_minutes=30, actual_minutes=10, clv=-0.1), RV.NEWS),
    (dict(news=False, proj_minutes=30, actual_minutes=21.9, clv=0.1), RV.MINUTES),  # >= 8 min
    (dict(news=False, proj_minutes=20, actual_minutes=14.9, clv=0.1), RV.MINUTES),  # >= 25%
    (dict(news=False, proj_minutes=30, actual_minutes=24, clv=-0.01), RV.MARKET),
    (dict(news=False, proj_minutes=30, actual_minutes=24, clv=0.01), RV.VARIANCE),
    (dict(news=None, proj_minutes=30, actual_minutes=30, clv=0.01), RV.NED),
    (dict(news=False, proj_minutes=None, actual_minutes=30, clv=0.01), RV.NED),
    (dict(news=False, proj_minutes=30, actual_minutes=30, clv=None), RV.NED),
])
def test_the_tag_is_the_first_rule_that_applies(kw, want):
    assert RV.tag(result="loss", player_pick=True, proj_snap=None, actual_snap=None, nfl=False,
                  **kw) == want  # fmt: skip
    assert RV.tag(result="void", player_pick=True, proj_snap=None, actual_snap=None, nfl=False,
                  **kw) == ""


def test_game_markets_skip_minutes_and_nfl_uses_snap_share():
    common = dict(result="win", news=False, proj_minutes=None, actual_minutes=None, clv=0.02)
    assert RV.tag(player_pick=False, proj_snap=None, actual_snap=None, nfl=False,
                  **common) == RV.VARIANCE  # fmt: skip
    assert RV.tag(player_pick=True, proj_snap=80, actual_snap=55, nfl=True,
                  **common) == RV.MINUTES  # fmt: skip
    assert RV.tag(player_pick=True, proj_snap=80, actual_snap=56, nfl=True,
                  **common) == RV.VARIANCE  # fmt: skip


def test_pick_time_statuses_and_a_change_after_the_pick(dbs, tmp_path):
    pdb, main = dbs
    _report(main, "Banchero, Paolo", "Questionable", PICK - timedelta(hours=1))
    _report(main, "Bench, Ben", "Out", PICK - timedelta(hours=1))
    _report(main, "Banchero, Paolo", "Out", PICK + timedelta(hours=2))  # after the pick
    _box(main, "Anthony Black", 31.0, 2.0)
    _pick(pdb, "k1")
    _sync(pdb, main, tmp_path)
    [rp] = L.read_rows(tmp_path / "review_pick.csv")
    assert (rp["team"], rp["opponent"], rp["proj_median"], rp["proj_minutes"]) == (
        "ORL", "MIA", "2.0", "30.0")
    # the player and his key teammate (34 min); the 12-minute bench player is not watched
    assert json.loads(rp["statuses"]) == {"Anthony Black": "Available",
                                          "Paolo Banchero": "Questionable"}
    [rr] = L.read_rows(tmp_path / "review_result.csv")
    assert (rr["actual_stat"], rr["actual_minutes"], rr["tag"]) == ("2.0", "31.0", RV.NEWS)
    assert json.loads(rr["status_changes"]) == [{"player": "Paolo Banchero",
                                                 "from": "Questionable", "to": "Out"}]
    assert rr["final_score"] == "ORL 104-101 MIA" and rr["clv"] == "2.0"
    row = next(r for r in O.dataset(tmp_path) if r["pick_id"] == rp["pick_id"])
    assert (row["team"], row["opponent"], row["actual_stat"], row["review_tag"]) == (
        "ORL", "MIA", "2.0", RV.NEWS)


def test_minutes_miss_market_and_variance(dbs, tmp_path):
    pdb, main = dbs
    _report(main, "Banchero, Paolo", "Available", PICK - timedelta(hours=1))
    _box(main, "Anthony Black", 18.0, 1.0)
    _pick(pdb, "minutes")  # projected 30, played 18: minutes miss
    _box(main, "Paolo Banchero", 35.0, 6.0)
    _pick(pdb, "market", player="Paolo Banchero", clv=-0.03,
          details={"edge": 0.1, "median": 5.0, "minutes": 34.0, "team_id": 1})
    _box(main, "Bam Adebayo", 33.0, 4.0, tid=2)
    _pick(pdb, "variance", player="Bam Adebayo", result="loss", profit=-1.0,
          details={"edge": 0.1, "median": 3.0, "minutes": 32.0, "team_id": 2})
    _pick(pdb, "void", player="Ben Bench", result="void", profit=0.0, clv=None)
    _pick(pdb, "pending", player="Paolo Banchero", result=None, market="rebounds")
    _sync(pdb, main, tmp_path)
    tags = {r["pick_id"]: r["tag"] for r in L.read_rows(tmp_path / "review_result.csv")}
    assert tags == {L.pick_id("minutes"): RV.MINUTES, L.pick_id("market"): RV.MARKET,
                    L.pick_id("variance"): RV.VARIANCE, L.pick_id("void"): ""}
    teams_ = {r["pick_id"]: (r["team"], r["opponent"])
              for r in L.read_rows(tmp_path / "review_pick.csv")}
    assert teams_[L.pick_id("variance")] == ("MIA", "ORL")
    # Ben Bench has no team_id in the alert: his team comes from his last box score
    assert teams_[L.pick_id("void")] == ("ORL", "MIA")


def test_a_game_market_pick(dbs, tmp_path):
    pdb, main = dbs
    _report(main, "Banchero, Paolo", "Available", PICK - timedelta(hours=1))
    _pick(pdb, "spread", player=None, market="spreads", side="away", line=1.5, details={})
    _sync(pdb, main, tmp_path)
    [rp] = L.read_rows(tmp_path / "review_pick.csv")
    assert (rp["team"], rp["opponent"]) == ("ORL", "MIA")
    # key players of both teams are watched
    assert set(json.loads(rp["statuses"])) == {"Paolo Banchero", "Anthony Black", "Bam Adebayo"}
    [rr] = L.read_rows(tmp_path / "review_result.csv")
    assert (rr["actual_stat"], rr["tag"]) == ("3.0", RV.VARIANCE)  # ORL won by 3


def test_review_rows_are_written_once(dbs, tmp_path):
    pdb, main = dbs
    _report(main, "Banchero, Paolo", "Available", PICK - timedelta(hours=1))
    _box(main, "Anthony Black", 31.0, 2.0)
    aid = _pick(pdb, "k1", result=None)
    _sync(pdb, main, tmp_path, PICK + timedelta(minutes=30))  # pick-time row only
    first = (tmp_path / "review_pick.csv").read_text()
    assert L.read_rows(tmp_path / "review_result.csv") == []  # not final yet
    with pdb.begin() as p:  # later: the alert's projection changes and the pick is graded
        p.execute(update(alerts).where(alerts.c.alert_id == aid).values(
            details={"median": 9.9, "minutes": 9.9, "team_id": 1}))
        p.execute(insert(alert_grades).values(alert_id=aid, result="win", profit=0.9,
                                              graded_at=TIP))  # fmt: skip
        p.execute(insert(consensus_grades).values(alert_id=aid, clv=0.01, graded_at=TIP))
    _sync(pdb, main, tmp_path)
    assert (tmp_path / "review_pick.csv").read_text() == first  # never rewritten
    results_ = (tmp_path / "review_result.csv").read_text()
    _sync(pdb, main, tmp_path, TIP + timedelta(days=1))
    assert (tmp_path / "review_result.csv").read_text() == results_
    with pytest.raises(L.AppendOnlyError):
        L.append_rows(tmp_path / "review_pick.csv", (*RV.PICK_FIELDS, "x"), [{"pick_id": "z"}])
