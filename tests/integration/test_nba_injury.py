"""M14 official NBA injury report: the PDF parser on saved real reports of each era, change-only
storage across versions (a status changed between two reports of the same day), `Removed`
rows, and the history and latest-report jobs."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import insert, select

from betdata.core.matching import sport_id
from betdata.db.schema import games, nba_injury_reports, teams
from betdata.nba.injury_pdf import parse_lines, pdf_lines
from betdata.sources.nba_injury import (
    NbaInjurySource,
    file_name,
    history_names,
    latest_names,
    load_report,
)
from tests.conftest import fixture_bytes

ET = ZoneInfo("America/New_York")


def report(name):
    return parse_lines(pdf_lines(fixture_bytes("nba_injury", f"Injury-Report_{name}.pdf")))


def test_parser_three_a_day_era():
    r = report("2019-01-10_05PM")
    assert r.report_at == datetime(2019, 1, 10, 17, 30)  # the stated time, not the file's hour
    baynes = next(x for x in r.rows if x["player_name"] == "Baynes, Aron")
    assert (baynes["game_date"], baynes["matchup"], baynes["team_name"], baynes["status"]) == (
        "2019-01-10", "BOS@MIA", "Boston Celtics", "Out")
    assert "Metacarpal" in baynes["reason"] and "Fracture" in baynes["reason"]  # wrapped
    harris = next(x for x in r.rows if x["player_name"] == "Harris, Gary")
    assert (harris["matchup"], harris["team_name"], harris["status"]) == (
        "LAC@DEN", "Denver Nuggets", "Questionable")
    assert {x["status"] for x in r.rows} <= {"Out", "Doubtful", "Questionable", "Probable",
                                            "Available"}  # fmt: skip
    assert len(r.rows) == 98


def test_parser_hourly_era_and_fifteen_minute_era():
    r = report("2023-03-20_05PM")
    assert r.report_at == datetime(2023, 3, 20, 17, 30)
    rows = {x["player_name"]: x for x in r.rows}
    assert rows["Caruso, Alex"]["status"] == "Out" and rows["Caruso, Alex"]["matchup"] == "CHI@PHI"
    assert rows["Green, Javonte"]["status"] == "Available"
    assert rows["Ball, Lonzo"]["reason"] == "Injury/Illness - Left Knee; Surgery"
    q = report("2026-02-11_05_00PM")
    assert q.report_at == datetime(2026, 2, 11, 17, 0)
    rows = {x["player_name"]: x for x in q.rows}
    assert rows["Daniels, Dyson"]["reason"] == "Injury/Illness - Right Ankle; Inflammation"
    assert rows["Curry, Stephen"]["status"] == "Out" and rows["Curry, Stephen"]["matchup"] == \
        "SAS@GSW"
    assert len(q.rows) == 162  # an 11-page report: rows carry across pages


def test_file_names_by_era():
    assert file_name(datetime(2019, 1, 10).date(), 17) == "Injury-Report_2019-01-10_05PM.pdf"
    assert file_name(datetime(2026, 2, 11).date(), 0, 15) == \
        "Injury-Report_2026-02-11_12_15AM.pdf"
    assert len(history_names(datetime(2020, 1, 1).date())) == 5
    assert history_names(datetime(2023, 3, 20).date())[0] == "Injury-Report_2023-03-20_11AM.pdf"
    now = datetime(2026, 10, 20, 21, 7, tzinfo=UTC)  # 5:07 PM EDT
    assert latest_names(now)[:2] == ["Injury-Report_2026-10-20_05_00PM.pdf",
                                     "Injury-Report_2026-10-20_04_45PM.pdf"]  # fmt: skip


def _games(engine, day, *matchups):
    with engine.begin() as conn:
        sid = sport_id(conn, "nba")
        ab = dict(conn.execute(select(teams.c.abbrev, teams.c.team_id)
                               .where(teams.c.sport_id == sid)).all())  # fmt: skip
        out = {}
        for m in matchups:
            away, home = m.split("@")
            tip = datetime.combine(day, datetime.min.time(), ET) + timedelta(hours=19)
            out[m] = conn.execute(insert(games).values(
                sport_id=sid, season=2022, start_time_utc=tip, home_team_id=ab[home],
                away_team_id=ab[away], status="final")).inserted_primary_key[0]  # fmt: skip
    return out


@pytest.fixture
def inj(engine, settings, fake_api, clock):
    return NbaInjurySource(engine, settings.source("nba_injury"), transport=fake_api.transport,
                           sleep=lambda s: None, clock=clock)  # fmt: skip


def test_history_keeps_every_version_change_only(inj, engine, fake_api, clock):
    day = datetime(2023, 3, 20).date()
    ids = _games(engine, day, "CHI@PHI", "DAL@MEM")
    clock.now = datetime(2026, 10, 20, tzinfo=UTC)
    for name in history_names(day):
        if name.endswith(("_01PM.pdf", "_05PM.pdf")):
            fake_api.add(body=fixture_bytes("nba_injury", name))
        else:
            fake_api.add(status=403, body="<Error>AccessDenied</Error>")
    res = inj.run("history", start="2023-03-20")
    assert res.status == "ok", res.message
    assert len(fake_api.requests) == 12  # hourly reports 11 AM - 10 PM
    with engine.connect() as conn:
        rows = conn.execute(select(nba_injury_reports).order_by(
            nba_injury_reports.c.report_at)).mappings().all()  # fmt: skip
    caruso = [r for r in rows if r["player_name"] == "Caruso, Alex"]
    assert [r["status"] for r in caruso] == ["Questionable", "Out"]  # changed the same day
    assert caruso[0]["report_at"] == datetime(2023, 3, 20, 13, 30, tzinfo=ET)
    assert caruso[1]["known_at"] - caruso[1]["report_at"] == timedelta(minutes=15)
    assert caruso[0]["game_id"] == ids["CHI@PHI"]
    doncic = [r["status"] for r in rows if r["player_name"] == "Doncic, Luka"]
    assert doncic == ["Questionable", "Out"]
    # Unchanged players appear once, at the report that first listed them.
    ball = [r for r in rows if r["player_name"] == "Ball, Lonzo"]
    assert len(ball) == 1
    # Loading the same day again adds nothing.
    for name in history_names(day):
        if name.endswith(("_01PM.pdf", "_05PM.pdf")):
            fake_api.add(body=fixture_bytes("nba_injury", name))
        else:
            fake_api.add(status=403, body="")
    assert inj.run("history", start="2023-03-20").rows_inserted == 0


def test_player_dropped_from_report_is_removed(engine):
    at = datetime(2023, 3, 20, 13, 30, tzinfo=ET)
    base = dict(game_date="2023-03-20", matchup="CHI@PHI", team_name="Chicago Bulls")
    first = {"report_at": at, "known_at": at + timedelta(minutes=15),
             "rows": [dict(base, player_name="A, B", status="Questionable", reason="x"),
                      dict(base, player_name="C, D", status="Out", reason="y")],
             "teams": [["2023-03-20", "CHI@PHI", "Chicago Bulls", True]]}  # fmt: skip
    later = {"report_at": at + timedelta(hours=4), "known_at": at + timedelta(hours=4, minutes=15),
             "rows": [dict(base, player_name="C, D", status="Out", reason="y")],
             "teams": [["2023-03-20", "CHI@PHI", "Chicago Bulls", True]]}  # fmt: skip
    with engine.begin() as conn:
        assert load_report(conn, first, fetched_at=at, raw_id=None) == 2
        assert load_report(conn, later, fetched_at=at, raw_id=None) == 1
        rows = conn.execute(select(nba_injury_reports.c.player_name,
                                   nba_injury_reports.c.status)
                            .order_by(nba_injury_reports.c.injury_report_id)).all()  # fmt: skip
    assert rows[-1] == ("A, B", "Removed")


def test_latest_report(inj, engine, fake_api, clock):
    clock.now = datetime(2026, 2, 11, 22, 10, tzinfo=UTC)  # 5:10 PM EST
    fake_api.add(body=fixture_bytes("nba_injury", "Injury-Report_2026-02-11_05_00PM.pdf"))
    res = inj.run("report")
    assert res.status == "ok", res.message
    assert fake_api.requests[0].url.path.endswith("Injury-Report_2026-02-11_05_00PM.pdf")
    with engine.connect() as conn:
        known = set(conn.execute(select(nba_injury_reports.c.known_at)).scalars())
    assert known == {clock.now}  # live: known when fetched


def test_report_without_games(engine):
    """A report listing no games (All-Star break) loads nothing and does not fail."""
    at = datetime(2025, 2, 14, 17, 30, tzinfo=ET)
    with engine.begin() as conn:
        assert load_report(conn, {"report_at": at, "known_at": at, "rows": [], "teams": []},
                           fetched_at=at, raw_id=None) == 0  # fmt: skip


def test_wrapped_team_name_and_title_lines():
    """'Minnesota' over 'Timberwolves' in the Team column is one team; a page title that falls
    inside the table is not a team."""
    lines = [[0, 300, 600, "Injury Report: 02/11/26 05:00 PM"],
             *[[0, x, 500, h] for x, h in ((20, "Game Date"), (120, "Game Time"),
                                           (200, "Matchup"), (265, "Team"),
                                           (425, "Player Name"), (585, "Current Status"),
                                           (666, "Reason"))],
             [0, 21, 480, "02/11/2026"], [0, 121, 480, "07:00 (ET)"], [0, 201, 480, "MIN@CHI"],
             [0, 266, 484, "Minnesota"], [0, 266, 476, "Timberwolves"],
             [0, 426, 480, "Edwards, Anthony"], [0, 586, 480, "Questionable"],
             [0, 667, 480, "Injury/Illness - Ankle"],
             [0, 266, 450, "Injury Report: 02/11/26 05:00 PM"],
             [0, 426, 452, "Gobert, Rudy"], [0, 586, 452, "Out"]]  # fmt: skip
    r = parse_lines(lines)
    assert [(x["team_name"], x["player_name"]) for x in r.rows] == [
        ("Minnesota Timberwolves", "Edwards, Anthony"),
        ("Minnesota Timberwolves", "Gobert, Rudy")]
