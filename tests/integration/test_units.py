"""nflverse team stats / snaps / rosters loading, and the unit model end to end (CLI)."""

import csv
import io
import random
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from betdata.config import get_settings
from betdata.db.schema import (
    model_predictions,
    player_game_snaps,
    roster_weeks,
    team_game_stats,
    teams,
)
from betdata.sources.nflverse import NflverseSource
from tests.conftest import fixture_bytes
from tests.integration.test_backtest import synthetic_csv

SAMPLE = fixture_bytes("nflverse", "games_sample.csv")


@pytest.fixture
def nflverse(engine, settings, fake_api, clock):
    return NflverseSource(
        engine, settings.source("nflverse"), transport=fake_api.transport,
        sleep=lambda s: None, clock=clock,
    )  # fmt: skip


def run(nflverse, fake_api, job, body, season):
    fake_api.add(body=body)
    result = nflverse.run(job, seasons=str(season))
    assert result.status == "ok", result.message
    return result


def rows(engine, table, *where):
    with engine.connect() as conn:
        return conn.execute(select(table).where(*where)).mappings().all()


def test_team_stats_snaps_and_rosters_load(nflverse, fake_api, engine):
    fake_api.add(body=SAMPLE)
    nflverse.run("schedules", seasons="all")

    r = run(
        nflverse,
        fake_api,
        "team_stats",
        fixture_bytes("nflverse", "stats_team_week_2024.csv"),
        2024,
    )
    assert (r.rows_inserted, r.rows_seen) == (4, 4)
    stats = {(s["source_game_id"], s["team_id"]): s for s in rows(engine, team_game_stats)}
    sb = [s for (gid, _), s in stats.items() if gid == "2024_22_KC_PHI"]
    assert len(sb) == 2 and all(s["game_id"] is not None for s in sb)  # linked to the schedule
    assert all(s["game_id"] is None for (gid, _), s in stats.items() if gid == "2024_01_ARI_BUF")
    assert all(s["pass_plays"] > 20 and s["rush_plays"] > 10 for s in stats.values())

    r = run(nflverse, fake_api, "snaps", fixture_bytes("nflverse", "snap_counts_2024.csv"), 2024)
    assert r.rows_inserted == 12
    hurts = rows(engine, player_game_snaps, player_game_snaps.c.player_name == "Jalen Hurts")[0]
    assert (hurts["position"], hurts["offense_pct"], hurts["defense_pct"]) == ("QB", 0.91, 0.0)

    r = run(
        nflverse, fake_api, "rosters", fixture_bytes("nflverse", "roster_weekly_2024.csv"), 2024
    )
    assert (r.rows_inserted, r.rows_seen) == (8, 8)  # the practice-squad row is not kept
    statuses = {x["player_name"]: x["status"] for x in rows(engine, roster_weeks)}
    assert statuses["C.J. Uzomah"] == "RES" and statuses["Jason Kelce"] == "RET"

    # Old team codes resolve (BLT = Ravens, SL = Rams...).
    r = run(
        nflverse, fake_api, "rosters", fixture_bytes("nflverse", "roster_weekly_2015.csv"), 2015
    )
    assert r.rows_inserted == 5
    with engine.connect() as conn:
        abbrevs = set(conn.execute(
            select(teams.c.abbrev).join(roster_weeks, roster_weeks.c.team_id == teams.c.team_id)
            .where(roster_weeks.c.season == 2015)
        ).scalars())  # fmt: skip
    assert abbrevs == {"BAL", "CLE", "HOU", "ARI"}  # ARZ appears twice

    # A reload replaces the season (stat corrections), it does not duplicate.
    run(nflverse, fake_api, "snaps", fixture_bytes("nflverse", "snap_counts_2024.csv") + b" ", 2024)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(player_game_snaps)).scalar() == 12


# --- Synthetic seasons for the end-to-end run ---------------------------------------------------

POSITIONS = ["QB", "T", "T", "G", "G", "C", "WR", "WR", "WR", "TE", "RB",
             "DE", "DE", "DT", "DT", "LB", "LB", "LB", "CB", "CB", "FS", "SS"]  # fmt: skip
OFFENSE = {"QB", "T", "G", "C", "WR", "TE", "RB"}


def _header(name):
    return next(csv.reader(io.StringIO(fixture_bytes("nflverse", name).decode())))


def context_files(schedule: bytes, seed=5) -> dict[tuple[str, int], bytes]:
    """Team stats, snaps, injury reports and rosters consistent with a schedule: every team has
    22 starters; each game a few are listed Out (and then play no snaps)."""
    rng = random.Random(seed)
    games = list(csv.DictReader(io.StringIO(schedule.decode())))
    files: dict[tuple[str, int], list[dict]] = {}
    pid = lambda t, i: (f"00-{t}{i:02}", f"{t}{i:02}X", f"{t} Player{i}")  # noqa: E731
    for g in games:
        season, week = int(g["season"]), int(g["week"])
        for team, opp in ((g["home_team"], g["away_team"]), (g["away_team"], g["home_team"])):
            out = set(rng.sample(range(len(POSITIONS)), rng.choice([0, 0, 1, 2])))
            for i, pos in enumerate(POSITIONS):
                gsis, pfr, name = pid(team, i)
                files.setdefault(("rosters", season), []).append(dict(
                    season=season, week=week, team=team, status="ACT", full_name=name,
                    gsis_id=gsis, pfr_id=pfr, position=pos, game_type="REG",
                ))  # fmt: skip
                if i in out:
                    files.setdefault(("injuries", season), []).append(dict(
                        season=season, game_type="REG", team=team, week=week, gsis_id=gsis,
                        position=pos, full_name=name, report_status="Out",
                        report_primary_injury="Knee", practice_status="Did Not Participate",
                        practice_primary_injury="Knee",
                    ))  # fmt: skip
                elif g["home_score"]:
                    share = round(rng.uniform(0.85, 1.0), 2)
                    files.setdefault(("snaps", season), []).append(dict(
                        game_id=g["game_id"], season=season, game_type="REG", week=week,
                        player=name, pfr_player_id=pfr, position=pos, team=team, opponent=opp,
                        offense_snaps=60, offense_pct=share if pos in OFFENSE else 0,
                        defense_snaps=60, defense_pct=0 if pos in OFFENSE else share,
                    ))  # fmt: skip
            if g["home_score"]:
                files.setdefault(("team_stats", season), []).append(dict(
                    season=season, week=week, team=team, season_type="REG",
                    game_id=g["game_id"], opponent_team=opp, attempts=33, sacks_suffered=2,
                    passing_epa=round(rng.gauss(2, 8), 3), passing_yards=230, carries=25,
                    rushing_epa=round(rng.gauss(-2, 4), 3), rushing_yards=110,
                ))  # fmt: skip
    headers = {"team_stats": _header("stats_team_week_2024.csv"),
               "snaps": _header("snap_counts_2024.csv"),
               "rosters": _header("roster_weekly_2024.csv"),
               "injuries": _header("injuries_2026.csv")}  # fmt: skip
    out = {}
    for (job, season), rs in files.items():
        buf = io.StringIO()
        w = csv.DictWriter(buf, headers[job], restval="")
        w.writeheader()
        w.writerows(rs)
        out[(job, season)] = buf.getvalue().encode()
    return out


def test_units_backtest_and_predict_cli(nflverse, fake_api, engine, monkeypatch, tmp_path):
    seasons = [2012, 2013, 2014, 2015, 2016]
    kickoff = date.today() + timedelta(days=2)
    # The upcoming game is in the last synthetic season, so its starters are known.
    schedule = synthetic_csv(seasons, upcoming_on=kickoff, upcoming_season=2016)
    fake_api.add(body=schedule)
    nflverse.run("schedules", seasons="all")
    for (job, season), body in sorted(context_files(schedule).items()):
        run(nflverse, fake_api, job, body, season)

    from betdata.cli import app

    monkeypatch.setenv("BETDATA_DB_URL", engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        out = runner.invoke(app, ["backtest", "nfl", "--model", "units", "--train", "2013-2015",
                                  "--test", "2016"])  # fmt: skip
        assert out.exit_code == 0, out.output
        expected = [
            "NFL unit model with missing starters", "Average miss on total points",
            "What a missing starter is worth", "cornerback", "Spreads (+missing):",
            "Live picks with these settings: betdata predict nfl --model units",
        ]  # fmt: skip
        for text in expected:
            assert text in out.output, text
        assert list((tmp_path / "data" / "reports").glob("backtest_nfl_units_*.csv"))

        out = runner.invoke(app, ["predict", "nfl", "--model", "units", "--train", "2013-2016"])
        assert out.exit_code == 0, out.output
        assert "KC @ BUF" in out.output and "total model" in out.output
        assert "BUF:" in out.output and "KC:" in out.output  # missing-starter lines
        with engine.connect() as conn:
            [p] = conn.execute(select(model_predictions)).mappings().all()
        assert p["model"] == "units+missing" and p["pred_total"] is not None
        assert p["market_spread_home"] is not None and p["market_total"] == 44.5
    finally:
        get_settings.cache_clear()
