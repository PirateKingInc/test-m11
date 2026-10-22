"""Issue #156: two real games between the same teams within 36 hours (an NBA two-game series)
stay two games across sources, and `betdata games split-merged` repairs games merged before."""

import csv
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from betdata.core.matching import find_or_create_game, sport_id
from betdata.core.models import ParsedEvent
from betdata.core.split_merged import merged_games, split_game
from betdata.core.teams_admin import record_events
from betdata.db.schema import (
    books,
    game_source_ids,
    games,
    markets,
    odds_snapshots,
    results,
    teams,
)
from betdata.sources.nba_cdn import NbaCdnSource
from betdata.sources.sportsdataverse import SportsDataverseSource
from tests.conftest import fixture_bytes

T1 = datetime(2027, 1, 15, 0, 0, tzinfo=UTC)
T2 = T1 + timedelta(hours=24)
SCORES = {"401900001": (T1, 101, 99), "401900002": (T2, 95, 110)}  # DET home, BOS away
CDN = {"0022600501": "401900001", "0022600502": "401900002"}


@pytest.fixture
def cdn(engine, settings, fake_api, clock):
    return NbaCdnSource(engine, settings.source("nba_cdn"), transport=fake_api.transport,
                        sleep=lambda s: None, clock=clock)  # fmt: skip


@pytest.fixture
def sdv(engine, settings, fake_api, clock):
    return SportsDataverseSource(engine, settings.source("sportsdataverse"),
                                 transport=fake_api.transport, sleep=lambda s: None,
                                 clock=clock)  # fmt: skip


def sdv_csv() -> bytes:
    rows = list(csv.DictReader(io.StringIO(
        fixture_bytes("sportsdataverse", "nba_schedule_2027.csv").decode())))  # fmt: skip
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0]))
    w.writeheader()
    for gid, (t, home, away) in SCORES.items():
        w.writerow({**rows[0], "id": gid, "game_id": gid,
                    "start_date": t.strftime("%Y-%m-%dT%H:%MZ"), "status_type_name": "STATUS_FINAL",
                    "home_score": home, "away_score": away, "season_type": "2"})  # fmt: skip
    return out.getvalue().encode()


def cdn_json(shift: timedelta) -> bytes:
    """The same two games from nba.com, with tip times `shift` off (a feed's other clock)."""
    data = json.loads(fixture_bytes("nba_cdn", "scheduleLeagueV2.json"))
    template = data["leagueSchedule"]["gameDates"][0]["games"][0]
    games_ = []
    for cid, espn in CDN.items():
        t, home, away = SCORES[espn]
        g = json.loads(json.dumps(template))
        g.update(gameId=cid, gameStatus=3, gameStatusText="Final",
                 gameDateTimeUTC=(t + shift).strftime("%Y-%m-%dT%H:%M:%SZ"))  # fmt: skip
        g["homeTeam"].update(teamTricode="DET", score=home)
        g["awayTeam"].update(teamTricode="BOS", score=away)
        games_.append(g)
    data["leagueSchedule"]["gameDates"] = [{"gameDate": "01/15/2027 00:00:00", "games": games_}]
    return json.dumps(data).encode()


def games_by(engine, source):
    with engine.connect() as conn:
        rows = conn.execute(
            select(game_source_ids.c.source_game_id, game_source_ids.c.game_id).where(
                game_source_ids.c.source == source)).all()  # fmt: skip
    return dict(rows)


def score(engine, game_id, source):
    with engine.connect() as conn:
        r = conn.execute(select(results.c.home_score, results.c.away_score).where(
            results.c.game_id == game_id, results.c.source == source)).one()  # fmt: skip
    return tuple(r)


@pytest.mark.parametrize("cdn_first", [False, True])
def test_two_game_series_stays_two_games(sdv, cdn, engine, fake_api, cdn_first):
    loads = [lambda: sdv.run("schedule", sport="nba"), lambda: cdn.run("schedule")]
    bodies = [sdv_csv(), cdn_json(timedelta(hours=2))]
    for i in ([1, 0] if cdn_first else [0, 1]):
        fake_api.add(body=bodies[i])
        assert loads[i]().status == "ok"
    by_sdv, by_cdn, by_espn = (games_by(engine, s) for s in ("sportsdataverse", "nba_cdn", "espn"))
    assert len(set(by_sdv.values())) == 2
    for cid, espn in CDN.items():
        gid = by_sdv[espn]
        assert by_cdn[cid] == gid and by_espn[espn] == gid  # same game across sources
        _, home, away = SCORES[espn]
        assert score(engine, gid, "sportsdataverse") == (home, away)
        assert score(engine, gid, "nba_cdn") == (home, away)
    with engine.connect() as conn:
        assert merged_games(conn) == []


def _team(conn, abbrev):
    return conn.execute(select(teams.c.team_id).where(teams.c.abbrev == abbrev)).scalar()


def _match(conn, source, gid, start, home="DET", away="BOS"):
    return find_or_create_game(conn, source=source, source_game_id=gid, sport="nba",
                               sport_id=sport_id(conn, "nba"), home_team_id=_team(conn, home),
                               away_team_id=_team(conn, away), start_time_utc=start)  # fmt: skip


def test_odds_event_takes_the_closest_kickoff(engine):
    with engine.begin() as conn:
        a = _match(conn, "sched", "A", T1).game_id
        b = _match(conn, "sched", "B", T2).game_id
        assert a != b
        assert _match(conn, "odds", "x2", T2 - timedelta(hours=3)).game_id == b
        assert _match(conn, "odds", "x1", T1 + timedelta(hours=5)).game_id == a
        swapped = _match(conn, "odds2", "y2", T2 + timedelta(hours=1), home="BOS", away="DET")
        assert (swapped.game_id, swapped.swapped) == (b, True)


def test_never_a_second_event_of_one_source(engine):
    with engine.begin() as conn:
        a = _match(conn, "odds", "x1", T1).game_id
        other = _match(conn, "odds", "x2", T1 + timedelta(hours=1))
        assert other.created and other.game_id != a
        assert _match(conn, "odds", "x1", T1).game_id == a  # known id: unchanged


def test_split_merged_repairs_an_old_merge(sdv, cdn, engine, fake_api):
    fake_api.add(body=sdv_csv())
    assert sdv.run("schedule", sport="nba").status == "ok"
    first, second = (games_by(engine, "sportsdataverse")[k] for k in SCORES)
    # Recreate the old matcher's merge: both events and their ESPN ids on the first game.
    with engine.begin() as conn:
        for src in ("sportsdataverse", "espn"):
            conn.execute(game_source_ids.update().where(
                game_source_ids.c.source == src, game_source_ids.c.source_game_id == "401900002")
                .values(game_id=first))  # fmt: skip
        conn.execute(results.delete().where(results.c.game_id == second))
        conn.execute(games.delete().where(games.c.game_id == second))
        conn.execute(results.update().where(results.c.game_id == first)
                     .values(home_score=95, away_score=110))  # the second game's score won
        assert merged_games(conn) == [first]
        fixed = split_game(conn, first)
        assert fixed.skipped is None and len(fixed.new_games) == 1
        assert merged_games(conn) == []
    by_sdv = games_by(engine, "sportsdataverse")
    assert by_sdv["401900001"] == first and by_sdv["401900002"] != first
    assert games_by(engine, "espn")["401900002"] == by_sdv["401900002"]
    fake_api.add(body=sdv_csv())  # reload refills the deleted results
    sdv.run("schedule", sport="nba")
    assert score(engine, first, "sportsdataverse") == (101, 99)
    assert score(engine, by_sdv["401900002"], "sportsdataverse") == (95, 110)


def test_split_skips_games_with_odds_unless_asked(engine):
    with engine.begin() as conn:
        a = _match(conn, "sched", "A", T1).game_id
        conn.execute(insert(game_source_ids).values(source="sched", source_game_id="B",
                                                    game_id=a))  # fmt: skip
        record_events(conn, source="sched", sport="nba", events=[
            ParsedEvent("A", T1, "Detroit Pistons", "Boston Celtics"),
            ParsedEvent("B", T2, "Detroit Pistons", "Boston Celtics")])  # fmt: skip
        book = conn.execute(select(books.c.book_id)).scalars().first()
        market = conn.execute(select(markets.c.market_id)).scalars().first()
        conn.execute(insert(odds_snapshots).values(
            source="sched", source_game_id="B", game_id=a, book_id=book, market_id=market,
            outcome="home", price_american=-110, price_decimal=1.91, fetched_at=T1,
            price_status="active", snapshot_kind="poll"))  # fmt: skip
        assert split_game(conn, a).skipped
        r = split_game(conn, a, relink_odds=True)
        assert r.skipped is None and r.relinked == 1
        moved = conn.execute(select(odds_snapshots.c.game_id)).scalar()
        assert moved == r.new_games[0] != a
