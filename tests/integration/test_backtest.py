"""Backtest dataset, CLI and stored predictions, on games loaded through the nflverse source."""

import csv
import io
import random
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from betdata.analysis.dataset import load_games
from betdata.analysis.models import EloModel
from betdata.analysis.report import predict_upcoming
from betdata.config import get_settings
from betdata.db.schema import model_predictions
from betdata.sources.nflverse import NflverseSource
from tests.conftest import fixture_bytes

SAMPLE = fixture_bytes("nflverse", "games_sample.csv")
HEADER = SAMPLE.decode().splitlines()[0].split(",")
TEAMS = ["BUF", "MIA", "NE", "NYJ", "KC", "LV", "DEN", "LAC"]


@pytest.fixture
def nflverse(engine, settings, fake_api, clock):
    return NflverseSource(
        engine, settings.source("nflverse"), transport=fake_api.transport,
        sleep=lambda s: None, clock=clock,
    )  # fmt: skip


def synthetic_csv(
    seasons, upcoming_on: date | None = None, seed=3, upcoming_season: int | None = None
) -> bytes:
    """A games.csv in nflverse's format: 8 teams, 10 weeks a season, random scores/lines."""
    rng = random.Random(seed)
    out = io.StringIO()
    w = csv.DictWriter(out, HEADER)
    w.writeheader()

    def row(season, week, day, away, home, scores=True):
        spread = rng.choice([-6.5, -3.0, -1.5, 1.0, 2.5, 3.0, 7.0])
        w.writerow({
            **dict.fromkeys(HEADER, ""), "game_id": f"{season}_{week:02}_{away}_{home}",
            "season": season, "game_type": "REG", "week": week, "gameday": day.isoformat(),
            "gametime": "13:00", "away_team": away, "home_team": home, "location": "Home",
            "away_score": rng.randint(3, 38) if scores else "",
            "home_score": rng.randint(3, 38) if scores else "",
            "spread_line": spread, "home_spread_odds": -110, "away_spread_odds": -110,
            "total_line": 44.5, "over_odds": -110, "under_odds": -110,
        })  # fmt: skip

    for season in seasons:
        for week in range(1, 11):
            order = rng.sample(TEAMS, len(TEAMS))
            day = date(season, 9, 9) + timedelta(weeks=week - 1)
            for i in range(0, len(TEAMS), 2):
                row(season, week, day, order[i], order[i + 1])
    if upcoming_on:
        season = upcoming_season or upcoming_on.year
        row(season, 11 if upcoming_season else 1, upcoming_on, "KC", "BUF", scores=False)
    return out.getvalue().encode()


def load(nflverse, fake_api, body, seasons="all"):
    fake_api.add(body=body)
    result = nflverse.run("schedules", seasons=seasons)
    assert result.status == "ok", result.message


def test_dataset_rows_from_the_nflverse_sample(nflverse, fake_api, engine):
    load(nflverse, fake_api, SAMPLE)
    with engine.connect() as conn:
        rows = {(g.season, g.home, g.away): g for g in load_games(conn)}
    sb = rows[(2024, "PHI", "KC")]
    assert sb.neutral and sb.final and sb.home_margin == 18
    assert (sb.spread_home, sb.spread_home_price, sb.spread_away_price) == (1.5, -115, -105)
    assert (sb.total, sb.over_price, sb.under_price) == (48.5, -115, -105)
    old = rows[(1999, "ATL", "MIN")]
    assert (old.spread_home, old.spread_home_price) == (4.0, None)  # 1999: line, no price
    upcoming = rows[(2026, "DEN", "SEA")]
    assert not upcoming.final and upcoming.spread_home == 2.5  # current line, not a close
    no_line = rows[(2026, "WAS", "DAL")]
    assert no_line.spread_home is None and no_line.total is None
    assert rows[(2025, "PHI", "DAL")].home_rest_days is None  # first game of the season


def test_dataset_rest_days(nflverse, fake_api, engine):
    load(nflverse, fake_api, synthetic_csv([2010]))
    with engine.connect() as conn:
        games = load_games(conn)
    week2 = [g for g in games if g.week == 2]
    assert week2 and all(g.home_rest_days == 7.0 and g.away_rest_days == 7.0 for g in week2)
    assert all(g.home_rest_days is None for g in games if g.week == 1)


def test_backtest_cli_end_to_end(nflverse, fake_api, engine, monkeypatch, tmp_path):
    load(nflverse, fake_api, synthetic_csv([2000, 2001, 2002, 2003]))
    from betdata.cli import app

    monkeypatch.setenv("BETDATA_DB_URL", engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        out = runner.invoke(app, ["backtest", "nfl", "--train", "2001-2002", "--test", "2003"])
        assert out.exit_code == 0, out.output
        assert "NFL spread backtest · elo:" in out.output
        assert "Verdict: INCONCLUSIVE" in out.output  # 80 games a season is far too few
        assert "Live picks with these settings: betdata predict nfl --model elo --k " in out.output
        [report] = (tmp_path / "data" / "reports").glob("backtest_nfl_elo_*.csv")
        rows = list(csv.DictReader(report.open()))
        assert {r["split"] for r in rows} <= {"train", "test"}
        assert all(r["result"] in ("win", "loss", "push") for r in rows)

        fixed = ["--k", "20", "--hfa", "48", "--revert", "0.33", "--bye", "0", "--threshold", "1"]
        out = runner.invoke(
            app, ["backtest", "nfl", "--train", "2001-2002", "--test", "2003", *fixed]
        )
        assert out.exit_code == 0, out.output
        assert "elo:bye=0,hfa=48,k=20,revert=0.33" in out.output and ">= 1 points" in out.output

        out = runner.invoke(app, ["backtest", "nfl", "--model", "market", "--train", "2001-2002",
                                  "--test", "2003"])  # fmt: skip
        assert out.exit_code == 0, out.output
        assert "test  2003                 0" in out.output  # never bets

        out = runner.invoke(app, ["backtest", "nfl", "--train", "2002-2003", "--test", "2003"])
        assert out.exit_code != 0  # overlapping seasons are refused
        out = runner.invoke(app, ["backtest", "nfl", "--train", "1990-1995", "--test", "2003"])
        assert out.exit_code == 1 and "no games with scores" in out.output
    finally:
        get_settings.cache_clear()


def test_predict_stores_only_changes(nflverse, fake_api, engine):
    kickoff = date(2026, 10, 18)
    load(nflverse, fake_api, synthetic_csv([2025], upcoming_on=kickoff))
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)

    def run(model):
        with engine.begin() as conn:
            games = load_games(conn)
            until = now + timedelta(days=8)
            return predict_upcoming(
                conn, games, model, now=now, until=until, lines_source="nflverse"
            )

    [first] = run(EloModel())
    assert first.stored and (first.game.home, first.game.away) == ("BUF", "KC")
    now += timedelta(hours=1)
    [again] = run(EloModel())
    assert not again.stored  # same prediction, same line
    [other] = run(EloModel(hfa=10))
    assert other.stored  # a different model version is its own history
    with engine.connect() as conn:
        rows = conn.execute(select(model_predictions)).mappings().all()
    assert len(rows) == 2
    assert {r["model_version"] for r in rows} == {
        "elo:bye=0,hfa=48,k=20,revert=0.33", "elo:bye=0,hfa=10,k=20,revert=0.33",
    }  # fmt: skip
    assert all(
        r["market_source"] == "nflverse" and r["market_spread_home"] is not None for r in rows
    )
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(model_predictions)).scalar() == 2


def test_predict_cli_smoke(nflverse, fake_api, engine, monkeypatch):
    load(nflverse, fake_api, synthetic_csv([2025], upcoming_on=date.today() + timedelta(days=2)))
    from betdata.cli import app

    monkeypatch.setenv("BETDATA_DB_URL", engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    try:
        out = CliRunner().invoke(app, ["predict", "nfl", "--model", "elo"])
        assert out.exit_code == 0, out.output
        units = CliRunner().invoke(app, ["predict", "nfl", "--model", "units"])  # no snap counts
        assert units.exit_code == 1 and "snap counts" in units.output
        m12 = CliRunner().invoke(app, ["predict", "nfl"])  # M12 is the default: no history yet
        assert m12.exit_code == 1 and "betdata nfl derive" in m12.output
        assert out.exit_code == 0, out.output
        assert "KC @ BUF" in out.output and "lean" in out.output
    finally:
        get_settings.cache_clear()
