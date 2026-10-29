"""M15 ESPN player prop lines on trimmed payloads: main-line parse rules (labelled sides,
DraftKings pairs, unpriced NFL pairs, anytime TD), paging, 404 providers, change-only storage,
history open/close rows, and NFL games matched to ESPN's scoreboard."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select

from betdata.core.matching import sport_id
from betdata.db.schema import (
    books,
    game_source_ids,
    games,
    prop_lines,
    raw_responses,
    teams,
)
from betdata.sources.espn_props import EspnPropsSource, main_line
from tests.conftest import fixture_bytes


def fx(name):
    return fixture_bytes("espn_props", name)


@pytest.fixture
def props(engine, settings, fake_api, clock):
    return EspnPropsSource(engine, settings.source("espn_props"), transport=fake_api.transport,
                           sleep=lambda s: None, clock=clock)  # fmt: skip


@pytest.fixture
def espn_odds_allowed(monkeypatch):
    """M18: ESPN odds/prop fetches refuse to run. These tests keep the retired fetch code's
    regression coverage (it documents the stored history's format) with the guard lifted."""
    import betdata.core.espn_odds as g

    monkeypatch.setattr(g, "ALLOWED", True)


def _game(engine, sport, home, away, tip, status="scheduled", espn=None, season=2026):
    with engine.begin() as conn:
        sid = sport_id(conn, sport)
        tid = dict(conn.execute(select(teams.c.canonical_name, teams.c.team_id)
                                .where(teams.c.sport_id == sid)).all())  # fmt: skip
        gid = conn.execute(insert(games).values(
            sport_id=sid, season=season, start_time_utc=tip, home_team_id=tid[home],
            away_team_id=tid[away], status=status)).inserted_primary_key[0]  # fmt: skip
        if espn:
            conn.execute(insert(game_source_ids).values(source="espn", source_game_id=espn,
                                                        game_id=gid))  # fmt: skip
    return gid


def stored(engine):
    with engine.connect() as conn:
        rows = conn.execute(select(prop_lines, books.c.key.label("book"))
                            .join(books, books.c.book_id == prop_lines.c.book_id)
                            .order_by(prop_lines.c.prop_line_id)).mappings().all()  # fmt: skip
    return {(r["player_id"], r["market"], r["book"], r["kind"]): r for r in rows}, rows


def test_main_line_rules():
    data = json.loads(fx("props_58.json"))["items"]
    pts = [i for i in data if "3995" in str(i.get("athlete")) and i["type"]["id"] == "1"]
    m = main_line("points", pts)  # labelled pair at 10.5; the 9.5 and 11.5 ladder is skipped
    assert m["close"] == (10.5, -110, -120) and m["open"] == (10.5, -110, -110)
    reb = [i for i in data if "4066" in str(i.get("athlete"))]
    assert main_line("rebounds", reb)["close"] == (7.5, 100, -130)  # "Even" is +100
    assert main_line("rebounds", reb)["open"] == (6.5, -115, -105)
    dk = json.loads(fx("props_100_p1.json"))["items"]
    m = main_line("points", dk)  # first of the pair is the over
    assert m["close"] == (8.5, -120, -106) and m["open"] == (8.5, 101, -128)
    assert main_line("points", dk[2:]) is None  # one-sided ladder only
    nfl = json.loads(fx("props_nfl.json"))["items"]
    assert main_line("rec_yds", nfl[:3])["close"] == (12.5, None, None)
    assert main_line("rec_yds", nfl[:3])["open"] == (14.5, None, None)
    assert main_line("anytime_td", nfl[3:4])["close"] == (None, 320, None)
    assert main_line("anytime_td", nfl[4:]) is None  # no price: skipped


def _nba_live(fake_api, p58="props_58.json"):
    fake_api.add(body=fx("providers_nba.json"))
    fake_api.add(body=fx(p58))
    fake_api.add(body=fx("props_100_p1.json")).add(body=fx("props_100_p2.json"))


def test_nba_lines_paging_and_change_only(props, engine, fake_api, clock, espn_odds_allowed):
    gid = _game(engine, "nba", "Los Angeles Lakers", "Golden State Warriors",
                datetime(2026, 10, 16, 2, 0, tzinfo=UTC), espn="401900001")  # fmt: skip
    _game(engine, "nba", "Boston Celtics", "New York Knicks",
          datetime(2026, 10, 19, 23, 0, tzinfo=UTC), espn="401900002")  # beyond 2 days
    _nba_live(fake_api)
    res = props.run("lines", sport="nba")
    assert res.status == "ok", res.message
    urls = [str(r.url) for r in fake_api.requests]
    assert len(urls) == 4 and "/odds/1004/" not in "".join(urls)  # consensus feed skipped
    assert "events/401900001/competitions/401900001/odds?" in urls[0]
    assert "/odds/100/propBets" in urls[2] and "page=2" in urls[3] and "limit=1000" in urls[3]
    by, rows = stored(engine)
    with engine.connect() as conn:  # one compact archived body per run
        raw = conn.execute(select(raw_responses.c.raw_id).where(
            raw_responses.c.source == "espn_props")).scalars().all()
    assert raw == [rows[0]["raw_id"]]
    assert len(rows) == 4  # 3995 points, 4066 rebounds (ESPN BET); 6478 points, assists (DK)
    r = by[("3995", "points", "espnbet", "poll")]
    assert (r["line"], r["over_price"], r["under_price"], r["game_id"]) == (10.5, -110, -120, gid)
    assert r["quoted_at"] == datetime(2025, 3, 16, 2, 20, tzinfo=UTC)
    r = by[("6478", "assists", "draftkings", "poll")]
    assert (r["line"], r["over_price"], r["under_price"]) == (3.5, 105, -135)

    clock.advance(hours=1)  # same data again: nothing new
    _nba_live(fake_api)
    assert props.run("lines", sport="nba").status == "ok"
    assert len(stored(engine)[1]) == 4

    clock.advance(hours=1)  # the under price moves: one new row
    moved = fx("props_58.json").replace(b'"-120"', b'"-125"')
    fake_api.add(body=fx("providers_nba.json")).add(body=moved)
    fake_api.add(body=fx("props_100_p1.json")).add(body=fx("props_100_p2.json"))
    assert props.run("lines", sport="nba").rows_inserted == 1
    assert stored(engine)[1][-1]["under_price"] == -125


def test_one_failing_game_does_not_stop_the_others(props, engine, fake_api, espn_odds_allowed):
    for hour, eid in ((20, "401900001"), (23, "401900002")):
        _game(engine, "nba", "Boston Celtics", "New York Knicks",
              datetime(2026, 10, 15, hour, tzinfo=UTC), espn=eid)  # fmt: skip
    fake_api.add(400, "bad")  # the first game's providers list fails
    _nba_live(fake_api)
    assert props.run("lines", sport="nba").status == "ok"
    assert len(fake_api.requests) == 5 and len(stored(engine)[1]) == 4


def test_nba_history_open_close_two_books_and_404(props, engine, fake_api, clock,
                                                  espn_odds_allowed):
    clock.now = datetime(2025, 4, 20, tzinfo=UTC)
    _game(engine, "nba", "Los Angeles Lakers", "Golden State Warriors",
          datetime(2025, 3, 16, 2, 30, tzinfo=UTC), status="final", espn="401705001",
          season=2024)  # fmt: skip
    listed = json.dumps({"items": [{"provider": {"id": i, "name": n}} for i, n in
                                   (("45", "Caesars Sportsbook (New Jersey)"), ("47", "MGM"),
                                    ("58", "ESPN BET"))]})  # fmt: skip
    fake_api.add(body=listed).add(404, "{}")  # ESPN BET first: no props for this game
    fake_api.add(body=fx("props_58.json")).add(body=fx("props_100_p1.json").replace(
        b'"pageCount": 2', b'"pageCount": 1'))  # fmt: skip
    res = props.run("history", sport="nba", season=2024)
    assert res.status == "ok", res.message
    urls = [str(r.url) for r in fake_api.requests]
    assert len(urls) == 4 and all(f"/odds/{p}/propBets" in u
                                  for p, u in zip(("58", "47", "45"), urls[1:], strict=True))
    by, rows = stored(engine)
    assert {k[2] for k in by} == {"betmgm", "caesars"}
    r, o = by[("4066", "rebounds", "betmgm", "close")], by[("4066", "rebounds", "betmgm", "open")]
    assert (r["line"], r["over_price"], r["under_price"]) == (7.5, 100, -130)
    assert (o["line"], o["over_price"], o["under_price"]) == (6.5, -115, -105)
    c = by[("6478", "points", "caesars", "open")]
    assert (c["line"], c["over_price"], c["under_price"]) == (8.5, 101, -128)
    assert props.run("history", sport="nba", season=2024).status == "skipped"  # done: resumable


def test_nfl_lines_match_scoreboard_and_unpriced_lines(props, engine, fake_api, clock,
                                                       espn_odds_allowed):
    clock.now = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)  # 11 ET: games 2-8 days out refresh
    gid = _game(engine, "nfl", "Kansas City Chiefs", "Washington Commanders",
                datetime(2026, 10, 25, 17, 0, tzinfo=UTC))  # fmt: skip
    fake_api.add(body=fx("scoreboard_nfl_20261025.json"))
    fake_api.add(body=fx("providers_nfl.json")).add(body=fx("props_nfl.json"))
    res = props.run("lines", sport="nfl")
    assert res.status == "ok", res.message
    assert "scoreboard?dates=20261025" in str(fake_api.requests[0].url)
    assert "football/leagues/nfl/events/401772900/" in str(fake_api.requests[1].url)
    with engine.connect() as conn:
        mapped = conn.execute(select(game_source_ids.c.source_game_id, game_source_ids.c.game_id)
                              .where(game_source_ids.c.source == "espn_nfl")).all()  # fmt: skip
    assert mapped == [("401772900", gid)]
    by, rows = stored(engine)
    assert len(rows) == 2
    r = by[("3054850", "rec_yds", "draftkings", "poll")]
    assert (r["line"], r["over_price"], r["under_price"]) == (12.5, None, None)
    assert by[("3054850", "anytime_td", "draftkings", "poll")]["over_price"] == 320

    clock.now = datetime(2026, 10, 21, 16, 0, tzinfo=UTC)  # noon ET: 4 days out, not refreshed
    assert props.run("lines", sport="nfl").status == "skipped" and len(fake_api.requests) == 3


def _labelled(side, line, price):
    return {"current": {side: {"american": price}, "target": {"value": line}},
            "open": {side: {"american": price}, "target": {"value": line}}}  # fmt: skip


def test_main_line_most_balanced_of_several_labelled_pairs():
    # M16 audit (REVIEW M16-1): a labelled alternate pair listed first was taken as the main line.
    items = [_labelled("over", 14.5, 300), _labelled("under", 14.5, -450),
             _labelled("over", 10.5, -112), _labelled("under", 10.5, -108)]  # fmt: skip
    assert main_line("points", items)["close"] == (10.5, -112, -108)
