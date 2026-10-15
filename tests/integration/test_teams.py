"""M4: college team import, name resolution, review queue, aliases, relink, multi-sport odds."""

import csv
import json

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from betdata.config import get_settings
from betdata.core.matching import sport_id
from betdata.core.normalize.teams import TeamResolver
from betdata.core.teams_admin import AliasConflict, add_alias, find_team, relink
from betdata.db.schema import (
    api_usage,
    games,
    odds_snapshots,
    source_events,
    team_aliases,
    team_source_ids,
    teams,
    unmatched_entities,
)
from betdata.db.seed import seed
from betdata.sources.cfbd import CfbdSource
from betdata.sources.the_odds_api import TheOddsApiSource
from tests.conftest import fixture_bytes

ODDS_HEADERS = {"x-requests-last": "3", "x-requests-remaining": "497", "x-requests-used": "3"}


@pytest.fixture
def cfbd(engine, settings, fake_api, clock, monkeypatch):
    monkeypatch.setenv("CFBD_API_KEY", "cfbd-test-key")
    return CfbdSource(
        engine, settings.source("cfbd"), transport=fake_api.transport,
        sleep=lambda s: None, clock=clock,
    )  # fmt: skip


@pytest.fixture
def odds_api(engine, settings, fake_api, clock):
    return TheOddsApiSource(
        engine, settings.source("the_odds_api"), transport=fake_api.transport,
        sleep=lambda s: None, clock=clock,
    )  # fmt: skip


def import_teams(cfbd, fake_api, which):
    fake_api.add(body=fixture_bytes("cfbd", f"teams_{which}.json"))
    result = cfbd.run(f"teams_{which}")
    assert result.status == "ok", result.message
    return result


def resolver(engine, sport):
    conn = engine.connect()
    return TeamResolver(conn, sport_id(conn, sport))


def count(engine, table, *where):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table).where(*where)).scalar()


# --- import --------------------------------------------------------------------


def test_football_import(cfbd, engine, fake_api):
    result = import_teams(cfbd, fake_api, "football")
    req = fake_api.requests[0]
    assert req.headers["authorization"] == "Bearer cfbd-test-key"
    assert req.url.path == "/teams" and req.url.params["year"] == "2026"
    assert req.url.host == "api.collegefootballdata.com"
    assert result.rows_seen == 9  # D-II Grand Valley State dropped, FCS kept
    with engine.connect() as conn:
        levels = dict(conn.execute(select(teams.c.canonical_name, teams.c.division_level)).all())
        usage = conn.execute(select(api_usage)).mappings().one()
    assert levels["Northern Iowa"] == "FCS" and "Grand Valley State" not in levels
    assert usage["source"] == "cfbd" and usage["endpoint"] == "football:/teams"
    assert cfbd.quota.status().used_effective == 1


def test_shared_alias_is_never_added(cfbd, engine, fake_api):
    import_teams(cfbd, fake_api, "football")
    # "Miami" is claimed by Miami (FL) and Miami (OH): it stays the canonical name of Miami
    # only, never an alias of Miami (OH).
    with engine.connect() as conn:
        rows = conn.execute(
            select(team_aliases.c.alias).where(team_aliases.c.alias == "Miami")
        ).all()
    assert rows == []
    r = resolver(engine, "ncaaf")
    assert r.lookup("the_odds_api", "Miami") == r.lookup("*", "Miami Hurricanes")
    assert r.lookup("the_odds_api", "Miami (OH) RedHawks") != r.lookup("*", "Miami Hurricanes")


@pytest.mark.parametrize(
    "odds_name,canonical",
    [
        ("Alabama Crimson Tide", "Alabama"),
        ("Miami Hurricanes", "Miami"),
        ("Miami (OH) RedHawks", "Miami (OH)"),
        ("USC Trojans", "USC"),
        ("South Carolina Gamecocks", "South Carolina"),
        ("San Jose State Spartans", "San José State"),  # accent dropped
        ("San José State Spartans", "San José State"),
        ("Hawaii Rainbow Warriors", "Hawai'i"),  # apostrophe dropped
        ("Kansas St Wildcats", None),  # not a known spelling: queued, never guessed
    ],
)
def test_odds_api_college_names(cfbd, engine, fake_api, odds_name, canonical):
    import_teams(cfbd, fake_api, "football")
    r = resolver(engine, "ncaaf")
    team_id = r.lookup("the_odds_api", odds_name)
    expected = r.lookup("*", canonical) if canonical else None
    assert team_id == expected


def test_abbreviations_are_source_specific(cfbd, engine, fake_api):
    import_teams(cfbd, fake_api, "football")
    r = resolver(engine, "ncaaf")
    assert r.lookup("cfbd", "SC") == r.lookup("*", "South Carolina")
    assert r.lookup("the_odds_api", "SC") is None


def test_basketball_import_keeps_espn_ids(cfbd, engine, fake_api):
    import_teams(cfbd, fake_api, "basketball")
    assert fake_api.requests[0].url.host == "api.collegebasketballdata.com"
    with engine.connect() as conn:
        espn = conn.execute(
            select(team_source_ids.c.source_team_id, teams.c.canonical_name)
            .join(teams)
            .where(team_source_ids.c.source == "espn")
        ).all()
    assert ("41", "UConn") in espn
    r = resolver(engine, "ncaab")
    assert r.lookup("the_odds_api", "UConn Huskies") == r.lookup("*", "UConn")
    # "St Johns Red Storm" normalizes to the same key as "St. John's Red Storm"
    assert r.lookup("the_odds_api", "St Johns Red Storm") == r.lookup("*", "St. John's")


def test_reimport_is_idempotent_and_updates_conference(cfbd, engine, fake_api):
    import_teams(cfbd, fake_api, "football")
    before = count(engine, team_aliases)
    body = fixture_bytes("cfbd", "teams_football.json").replace(b'"Mountain West"', b'"Pac-12"')
    fake_api.add(body=body)
    assert cfbd.run("teams_football").rows_inserted == 0
    assert count(engine, team_aliases) == before
    with engine.connect() as conn:
        conf = conn.execute(
            select(teams.c.conference).where(teams.c.canonical_name == "San José State")
        ).scalar()
    assert conf == "Pac-12"


def test_missing_key_fails_cleanly(engine, settings, fake_api, monkeypatch):
    monkeypatch.delenv("CFBD_API_KEY", raising=False)
    src = CfbdSource(engine, settings.source("cfbd"), transport=fake_api.transport)
    result = src.run("teams_football")
    assert result.status == "failed" and "CFBD_API_KEY" in result.message
    assert fake_api.requests == []


# --- review queue, aliases, relink ----------------------------------------------


def ncaab_board(*matchups):
    events = []
    for i, (home, away) in enumerate(matchups):
        events.append({
            "id": f"ncaab{i}", "sport_key": "basketball_ncaab",
            "commence_time": "2026-11-20T00:00:00Z", "home_team": home, "away_team": away,
            "bookmakers": [{"key": "draftkings", "title": "DraftKings", "markets": [
                {"key": "h2h", "outcomes": [{"name": home, "price": -150},
                                            {"name": away, "price": 130}]}]}],
        })  # fmt: skip
    return json.dumps(events)


def test_unmatched_then_accept_then_relink(cfbd, odds_api, engine, fake_api, isolated_seeds):
    import_teams(cfbd, fake_api, "basketball")
    fake_api.add(
        body=ncaab_board(("Saint Marys (CA) Gaels", "UConn Huskies")), headers=ODDS_HEADERS
    )
    assert odds_api.run("odds", sport="ncaab").rows_inserted == 2

    # odds are kept even though one team is unknown; no game yet
    assert count(engine, games) == 0
    assert count(engine, odds_snapshots, odds_snapshots.c.game_id.is_(None)) == 2
    with engine.connect() as conn:
        u = conn.execute(select(unmatched_entities)).mappings().one()
        assert conn.execute(select(func.count()).select_from(source_events)).scalar() == 1
    assert u["raw_value"] == "Saint Marys (CA) Gaels"
    smc = find_team(engine.connect(), "ncaab", "Saint Mary's")
    assert u["suggested_team_id"] == smc

    with engine.begin() as conn:
        add_alias(conn, sport="ncaab", source="the_odds_api", alias=u["raw_value"], team_id=smc)
        stats = relink(conn)
    assert (stats.events_linked, stats.snapshots_linked) == (1, 2)
    assert count(engine, games) == 1
    assert count(engine, odds_snapshots, odds_snapshots.c.game_id.is_(None)) == 0
    assert count(engine, unmatched_entities, unmatched_entities.c.resolved.is_(False)) == 0

    # persisted for rebuilds, in the (test copy of the) seeds file
    rows = list(csv.DictReader((isolated_seeds / "team_aliases.csv").open()))
    assert rows[-1] == {
        "sport": "ncaab",
        "source": "the_odds_api",
        "alias": "Saint Marys (CA) Gaels",
        "canonical_name": "Saint Mary's",
    }


def test_alias_conflict(cfbd, engine, fake_api):
    import_teams(cfbd, fake_api, "basketball")
    with engine.begin() as conn:
        uconn = find_team(conn, "ncaab", "UConn")
        kansas = find_team(conn, "ncaab", "Kansas")
        add_alias(conn, sport="ncaab", source="*", alias="Huskies", team_id=uconn, persist=False)
        with pytest.raises(AliasConflict):
            add_alias(
                conn, sport="ncaab", source="*", alias="Huskies", team_id=kansas, persist=False
            )


def test_saved_aliases_wait_for_imported_teams(cfbd, engine, fake_api, isolated_seeds):
    with (isolated_seeds / "team_aliases.csv").open("a") as f:
        f.write("ncaab,the_odds_api,Saint Marys (CA) Gaels,Saint Mary's\n")
    assert seed(engine, isolated_seeds)["aliases_pending"] == 1  # team not imported yet
    import_teams(cfbd, fake_api, "basketball")
    assert seed(engine, isolated_seeds)["aliases_pending"] == 0
    r = resolver(engine, "ncaab")
    assert r.lookup("the_odds_api", "Saint Marys (CA) Gaels") == r.lookup("*", "Saint Mary's")


def test_participants_job_queues_unknown_names(cfbd, odds_api, engine, fake_api):
    import_teams(cfbd, fake_api, "basketball")
    fake_api.add(body=fixture_bytes("the_odds_api", "ncaab_participants.json"),
                 headers={"x-requests-last": "1"})  # fmt: skip
    result = odds_api.run("participants", sport="ncaab")
    assert fake_api.requests[-1].url.path == "/v4/sports/basketball_ncaab/participants"
    assert (result.rows_inserted, result.rows_seen) == (2, 5)
    with engine.connect() as conn:
        pending = set(conn.execute(select(unmatched_entities.c.raw_value)).scalars())
    assert pending == {"Saint Marys (CA) Gaels", "Gonzaga Bulldogs"}
    assert odds_api.quota.status().used_effective == 1


def test_cli_review_flow(cfbd, odds_api, engine, fake_api, monkeypatch):
    import_teams(cfbd, fake_api, "basketball")
    fake_api.add(
        body=ncaab_board(("Saint Marys (CA) Gaels", "UConn Huskies")), headers=ODDS_HEADERS
    )
    odds_api.run("odds", sport="ncaab")

    from betdata.cli import app

    monkeypatch.setenv("BETDATA_DB_URL", str(engine.url))
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        out = runner.invoke(app, ["unmatched"])
        assert out.exit_code == 0 and "Saint Marys (CA) Gaels" in out.output
        assert "Saint Mary's" in out.output
        uid = out.output.split()[0]
        out = runner.invoke(app, ["alias", "accept", uid, "--yes"])
        assert out.exit_code == 0, out.output
        assert "relinked 1 events, 2 odds rows" in out.output
        out = runner.invoke(app, ["alias", "add", "ncaab", "Zags", "Nonexistent U"])
        assert out.exit_code != 0
    finally:
        get_settings.cache_clear()


# --- NBA ---------------------------------------------------------------------------


def test_nba_names(engine):
    r = resolver(engine, "nba")
    clippers = r.lookup("*", "Los Angeles Clippers")
    assert clippers is not None
    assert r.lookup("the_odds_api", "LA Clippers") == clippers
    assert r.lookup("espn", "GS") == r.lookup("*", "Golden State Warriors")
    assert r.lookup("x", "Seattle SuperSonics") == r.lookup("*", "Oklahoma City Thunder")
