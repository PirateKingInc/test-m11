"""M14 NBA data from ESPN and sportsdataverse, on saved real payloads: the live scoreboard
(games, scores, DraftKings lines), box scores (season file and per-game summary), and historical
opening/closing lines."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, insert, select

from betdata.core.matching import sport_id
from betdata.db.schema import (
    game_source_ids,
    games,
    nba_player_games,
    odds_snapshots,
    results,
    teams,
)
from betdata.sources.espn_nba import EspnNbaSource, core_odds, parse_odds_history
from tests.conftest import fixture_bytes


@pytest.fixture
def espn(engine, settings, fake_api, clock):
    return EspnNbaSource(engine, settings.source("espn_nba"), transport=fake_api.transport,
                         sleep=lambda s: None, clock=clock)  # fmt: skip


def count(engine, table, *where):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table).where(*where)).scalar()


def espn_game(engine, espn_id):
    with engine.connect() as conn:
        return conn.execute(
            select(games).join(game_source_ids).where(
                game_source_ids.c.source == "espn", game_source_ids.c.source_game_id == espn_id)
        ).mappings().one()  # fmt: skip


def test_scoreboard_games_and_draftkings_lines(espn, engine, fake_api):
    fake_api.add(body=fixture_bytes("espn_nba", "scoreboard_20261021.json"))
    res = espn.run("scoreboard", dates="20261021")
    assert res.status == "ok", res.message
    assert "dates=20261021" in str(fake_api.requests[0].url) and len(fake_api.requests) == 1
    g = espn_game(engine, "401909834")  # ATL @ ORL, Oct 21 2026
    assert (g["season"], g["season_type"], g["status"]) == (2026, "REG", "scheduled")
    with engine.connect() as conn:
        rows = conn.execute(
            select(odds_snapshots.c.snapshot_kind, odds_snapshots.c.outcome,
                   odds_snapshots.c.line, odds_snapshots.c.price_american)
            .where(odds_snapshots.c.game_id == g["game_id"])).all()  # fmt: skip
    by = {(r.snapshot_kind, r.outcome, r.line): r.price_american for r in rows}
    assert by[("poll", "home", -3.5)] == -108 and by[("poll", "away", 3.5)] == -112
    assert by[("poll", "home", None)] == -142 and by[("poll", "away", None)] == 120
    assert by[("source_opening", "home", -2.5)] == -110  # the opening spread
    assert by[("poll", "over", 236.5)] == -105


def test_old_scoreboard_final_scores(espn, engine, fake_api):
    fake_api.add(body=fixture_bytes("espn_nba", "scoreboard_20160105.json"))
    assert espn.run("scoreboard", dates="20160105").status == "ok"
    g = espn_game(engine, "400828408")
    assert (g["season"], g["status"]) == (2015, "final")
    assert count(engine, results) == 2


def _game(engine, espn_id, home, away, tip, status="final"):
    with engine.begin() as conn:
        sid = sport_id(conn, "nba")
        tid = dict(conn.execute(select(teams.c.canonical_name, teams.c.team_id)
                                .where(teams.c.sport_id == sid)).all())  # fmt: skip
        gid = conn.execute(insert(games).values(
            sport_id=sid, season=2023, start_time_utc=tip, home_team_id=tid[home],
            away_team_id=tid[away], status=status)).inserted_primary_key[0]  # fmt: skip
        conn.execute(insert(game_source_ids).values(source="espn", source_game_id=espn_id,
                                                    game_id=gid))  # fmt: skip
    return gid


def test_box_scores_from_game_summary(espn, engine, fake_api, clock):
    tip = datetime(2024, 3, 17, 0, 30, tzinfo=UTC)
    clock.now = datetime(2024, 3, 17, 4, 0, tzinfo=UTC)
    gid = _game(engine, "401585601", "Los Angeles Lakers", "Golden State Warriors", tip)
    fake_api.add(body=fixture_bytes("espn_nba", "summary_401585601.json"))
    res = espn.run("boxscores", days=2)
    assert res.status == "ok", res.message
    assert "event=401585601" in str(fake_api.requests[0].url)
    with engine.connect() as conn:
        rows = conn.execute(select(nba_player_games).where(
            nba_player_games.c.game_id == gid)).mappings().all()  # fmt: skip
    assert len(rows) >= 20 and sum(r["starter"] for r in rows) == 10
    assert all(r["known_at"] == clock.now for r in rows)  # live: known when fetched
    assert any(r["plus_minus"] not in (None, 0) for r in rows)
    played = [r for r in rows if r["minutes"]]
    assert sum(r["points"] for r in played) == sum(2 * r["fgm"] + r["fg3m"] + r["ftm"]
                                                   for r in played)  # fmt: skip
    # Nothing left to fetch: the next run makes no request.
    before = len(fake_api.requests)
    espn.run("boxscores", days=2)
    assert len(fake_api.requests) == before


def test_odds_history_open_and_close(espn, engine, fake_api, clock):
    tip = datetime(2024, 3, 17, 0, 30, tzinfo=UTC)
    clock.now = datetime(2024, 3, 20, tzinfo=UTC)
    gid = _game(engine, "401585601", "Los Angeles Lakers", "Golden State Warriors", tip)
    fake_api.add(body=fixture_bytes("espn_nba", "odds_401585601.json"))
    res = espn.run("odds_history", season=2023)
    assert res.status == "ok", res.message
    assert "/events/401585601/competitions/401585601/odds" in str(fake_api.requests[0].url)
    with engine.connect() as conn:
        rows = conn.execute(
            select(odds_snapshots.c.snapshot_kind, odds_snapshots.c.outcome,
                   odds_snapshots.c.line, odds_snapshots.c.price_american,
                   odds_snapshots.c.book_id)
            .where(odds_snapshots.c.game_id == gid, odds_snapshots.c.market_id == 2)
        ).all()  # fmt: skip
    close = {r.line for r in rows if r.snapshot_kind == "source_closing" and r.outcome == "home"}
    opening = {r.line for r in rows if r.snapshot_kind == "source_opening"}
    assert -1.5 in close and opening & {-3.0, -3.5}
    assert count(engine, odds_snapshots, odds_snapshots.c.snapshot_kind == "poll") == 0
    # Resumable: the game now has lines, so the next run asks for nothing.
    before = len(fake_api.requests)
    espn.run("odds_history", season=2023)
    assert len(fake_api.requests) == before


def test_odds_history_2015_has_opening_pseudo_book():
    data = json.loads(fixture_bytes("espn_nba", "odds_400827888.json"))
    parsed = parse_odds_history(json.dumps({"games": [core_odds("400827888", data)]}).encode(),
                                datetime(2026, 10, 20, tzinfo=UTC))  # fmt: skip
    opening = [p for p in parsed.prices if p.book_key == "opening"]
    assert {(p.market_key, p.outcome, p.line) for p in opening} >= {("spreads", "home", -8.0)}
    assert all(p.snapshot_kind == "source_opening" for p in opening)
    assert {p.book_key for p in parsed.prices} >= {"bovada", "betonline", "5dimes"}
    assert "consensus" not in {p.book_key for p in parsed.prices}


def test_history_export_and_load_round_trip(espn, engine, fake_api, clock, tmp_path):
    from sqlalchemy import delete

    from betdata.db.schema import nba_injury_reports
    from betdata.nba.history import export, load
    from betdata.sources.nba_injury import load_report

    tip = datetime(2024, 3, 17, 0, 30, tzinfo=UTC)
    clock.now = datetime(2024, 3, 20, tzinfo=UTC)
    gid = _game(engine, "401585601", "Los Angeles Lakers", "Golden State Warriors", tip)
    fake_api.add(body=fixture_bytes("espn_nba", "odds_401585601.json"))
    espn.run("odds_history", season=2023)
    at = datetime(2024, 3, 16, 17, 30, tzinfo=UTC)
    with engine.begin() as conn:
        load_report(conn, {"report_at": at, "known_at": at, "teams": [], "rows": [dict(
            game_date="2024-03-16", matchup="GSW@LAL", team_name="Los Angeles Lakers",
            player_name="James, LeBron", status="Questionable", reason="Ankle")]},
            fetched_at=at, raw_id=None)  # fmt: skip
    n_odds = count(engine, odds_snapshots)
    out = export(engine, tmp_path / "h", 2023)
    assert out == {"odds": n_odds, "injuries": 1}
    with engine.begin() as conn:
        conn.execute(delete(odds_snapshots))
        conn.execute(delete(nba_injury_reports))
    assert load(engine, tmp_path / "h") == {"odds": n_odds, "injuries": 1}
    assert load(engine, tmp_path / "h") == {"odds": 0, "injuries": 0}  # idempotent
    with engine.connect() as conn:
        row = conn.execute(select(nba_injury_reports)).mappings().one()
    assert row["game_id"] == gid and row["report_at"] == at


def test_scoreboard_asks_one_day_at_a_time(espn, engine, fake_api, clock):
    """ESPN answers 400 to a date range, so a range is fetched day by day and merged."""
    from betdata.sources.espn_nba import scoreboard_days

    clock.now = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
    for _ in range(4):
        fake_api.add(body=fixture_bytes("espn_nba", "scoreboard_20261021.json"))
    assert espn.run("scoreboard").status == "ok"  # default: yesterday to 2 days ahead
    assert [r.url.params["dates"] for r in fake_api.requests] == [
        "20261020", "20261021", "20261022", "20261023"]
    assert len(scoreboard_days("20261001-20261003", None)) == 3
