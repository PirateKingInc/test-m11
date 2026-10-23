"""Issue #157: `betdata lines coverage` on a synthetic database."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select
from typer.testing import CliRunner

from betdata.core.matching import sport_id
from betdata.db.schema import books, games, ingest_runs, markets, odds_snapshots, prop_lines, teams
from betdata.lines_coverage import coverage, report

NOW = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
PAST = NOW - timedelta(days=1)  # tip of the finished game
NEXT = NOW + timedelta(days=1)  # tip of the upcoming game


def _id(conn, table, col, value):
    return conn.execute(select(table.c[col]).where(table.c.key == value)).scalar()


def build(engine):
    with engine.begin() as conn:
        nba = sport_id(conn, "nba")
        team = dict(conn.execute(select(teams.c.abbrev, teams.c.team_id).where(
            teams.c.sport_id == nba)).all())  # fmt: skip
        gid = {}
        for name, t, h, a in (("past", PAST, "BOS", "NYK"), ("next", NEXT, "LAL", "GSW"),
                              ("old", NOW - timedelta(days=30), "MIA", "ORL")):  # fmt: skip
            gid[name] = conn.execute(insert(games).values(
                sport_id=nba, season=2026, start_time_utc=t, home_team_id=team[h],
                away_team_id=team[a], status="final" if t < NOW else "scheduled")
            ).inserted_primary_key[0]  # fmt: skip
        dk, fd = (conn.execute(select(books.c.book_id).where(books.c.key == k)).scalar()
                  for k in ("draftkings", "fanduel"))  # fmt: skip
        mk = {k: _id(conn, markets, "market_id", k) for k in ("spreads", "h2h", "totals")}

        def snap(game, source, book, market, kind, at, outcome="home"):
            conn.execute(insert(odds_snapshots).values(
                source=source, source_game_id=f"{source}-{game}", game_id=gid[game],
                book_id=book, market_id=mk[market], outcome=outcome, line=-3.5,
                price_american=-110, price_decimal=1.91, snapshot_kind=kind,
                fetched_at=at))  # fmt: skip

        # Finished game: DraftKings polled from the open to 20 min before tip on all markets,
        # plus an in-game change; FanDuel only has a source opening spread.
        for m in ("spreads", "h2h", "totals"):
            snap("past", "espn_nba", dk, m, "poll", PAST - timedelta(hours=20))
            snap("past", "espn_nba", dk, m, "poll", PAST + timedelta(minutes=30), "away")
        snap("past", "espn_nba", fd, "spreads", "source_opening", PAST - timedelta(hours=20))
        # Upcoming game: spreads and h2h polled, no total yet.
        for m in ("spreads", "h2h"):
            snap("next", "sharpapi", dk, m, "poll", NOW - timedelta(hours=2))
        for t in (PAST - timedelta(hours=6), PAST - timedelta(minutes=20), NOW):
            conn.execute(insert(ingest_runs).values(source="espn_nba", job="scoreboard",
                                                    started_at=t, status="ok"))  # fmt: skip

        def prop(game, kind, at, quoted=None):
            conn.execute(insert(prop_lines).values(
                source="espn_props", source_game_id=f"p-{game}", game_id=gid[game],
                player_id="1", market="points", book_id=dk, kind=kind, line=20.5,
                over_price=-110, under_price=-110, quoted_at=quoted, fetched_at=at))  # fmt: skip

        prop("past", "poll", PAST - timedelta(hours=3))
        prop("past", "poll", PAST - timedelta(hours=1))
        prop("next", "open", NOW, quoted=NOW + timedelta(hours=3))
        prop("next", "close", NOW, quoted=NEXT + timedelta(minutes=30))  # in-game: not a close
        snap("old", "espn_nba", dk, "spreads", "poll", NOW - timedelta(days=31))  # out of range


def test_coverage_counts(engine):
    build(engine)
    with engine.connect() as conn:
        cov = coverage(conn, "nba", now=NOW, days=3)
    assert (len(cov.started), len(cov.upcoming)) == (1, 1)
    r = cov.lines[("espn_nba", "draftkings", "spreads")]
    assert (r.games, r.open, r.current, r.started, r.close) == (1, 1, 1, 1, 1)
    assert r.tip_gaps == [20.0]
    fd = cov.lines[("espn_nba", "fanduel", "spreads")]
    assert (fd.open, fd.current, fd.close) == (1, 0, 0)  # an opening line is not a close
    up = cov.lines[("sharpapi", "draftkings", "h2h")]
    assert (up.games, up.current, up.started) == (1, 1, 0)
    assert cov.no_close == {"spreads": [], "h2h": [], "totals": []}
    assert [len(cov.no_current[m]) for m in ("spreads", "h2h", "totals")] == [0, 0, 1]
    assert not cov.complete
    p = cov.props[("espn_props", "draftkings")]
    assert (p.games, p.open, p.polls, p.started, p.close) == (2, 2, 2, 1, 1)
    text = "\n".join(report(cov))
    assert ("totals: started without a pre-game close 0/1; "
            "upcoming without a current line 1/1") in text


def test_cli_exit_code_only_with_strict(engine, monkeypatch):
    from betdata.cli import app
    from betdata.config import get_settings

    build(engine)
    monkeypatch.setenv("BETDATA_DB_URL", engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    try:
        args = ["lines", "coverage", "--sport", "nba", "--days", "3", "--at", NOW.isoformat()]
        out = CliRunner().invoke(app, args)
        assert out.exit_code == 0, out.output
        assert "espn_nba       draftkings   spreads" in out.output
        assert CliRunner().invoke(app, [*args, "--strict"]).exit_code == 1
    finally:
        get_settings.cache_clear()
