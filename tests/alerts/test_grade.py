"""M16 grading (issue #167): closes, CLV in points and no-vig, results, idempotency, reports."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from betdata.alerts import grade as G
from betdata.alerts.db import alert_grades, alerts, engine_for, quotes
from betdata.core.matching import sport_id
from betdata.db.schema import (
    books,
    games,
    markets,
    nba_player_games,
    odds_snapshots,
    prop_lines,
    results,
    teams,
)

TIP = datetime(2026, 10, 25, 23, 30, tzinfo=UTC)
NOW = TIP + timedelta(hours=4)


# --- pure math ---------------------------------------------------------------------------------


def test_clv_points_by_market_and_side():
    assert G.clv_points("spreads", "home", -3.5, -5.5) == 2.0  # took -3.5, closed -5.5
    assert G.clv_points("spreads", "away", 3.5, 5.5) == -2.0  # dog +3.5 closed +5.5
    assert G.clv_points("totals", "over", 220.5, 223.0) == 2.5
    assert G.clv_points("totals", "under", 220.5, 223.0) == -2.5
    assert G.clv_points("points", "over", 20.5, 22.5) == 2.0
    assert G.clv_points("points", "under", 20.5, 22.5) == -2.0
    assert G.clv_points("h2h", "home", None, None) is None


def test_sigmas_and_line_conversion():
    assert G.sigma("nba", "spreads", -3.5) == 12.0 and G.sigma("nfl", "totals", 44.5) == 13.0
    assert G.sigma("nba", "points", 25.5) == pytest.approx(0.5 * 25.5**0.5 + 0.15 * 25.5)
    assert G.sigma("nba", "threes", 0.5) == 1.0  # floor
    assert G.at_bet_line(0.5, 0.0, 12.0) == pytest.approx(0.5)
    assert G.at_bet_line(0.5, 2.0, 12.0) == pytest.approx(0.56619, abs=1e-5)  # Phi(2/12)
    assert G.at_bet_line(0.6, -3.0, 12.0) < 0.6


class A:  # an alert row
    def __init__(self, **kw):
        d = dict(signal="sharp", sport="nba", market="spreads", side="home", line=-3.5,
                 price=-110, book="draftkings", book_prob=0.48, details=None, player_name=None)
        d.update(kw)
        self.__dict__.update(d)


def test_clv_novig_same_and_moved_line():
    same = G.Close("draftkings", -3.5, -130, 110, TIP)  # close no-vig home ~0.543
    assert G.clv_novig(A(), same) == pytest.approx(0.5428 - 0.48, abs=1e-3)
    moved = G.Close("draftkings", -5.5, -110, -110, TIP)  # 50% at -5.5 -> ~56.6% at -3.5
    assert G.clv_novig(A(), moved) == pytest.approx(0.5662 - 0.48, abs=1e-3)
    # under 20.5 vs a close of under 22.5 at 50%: a worse line, below 50% at 20.5
    under = A(market="points", side="under", line=20.5, book_prob=0.5)
    assert G.clv_novig(under, G.Close("draftkings", 22.5, -110, -110, TIP)) < 0
    assert G.clv_novig(A(), G.Close("draftkings", -3.5, -110, None, TIP)) is None  # one side
    # no book_prob: the alert's own pair from details, else nothing
    assert G.clv_novig(A(book_prob=None, details={"other_price": -110}), same) == \
        pytest.approx(0.5428 - 0.5, abs=1e-3)
    assert G.clv_novig(A(book_prob=None), same) is None


def test_results_and_profit():
    assert G.result_of("spreads", "home", -3.5, 110, 105) == "win"
    assert G.result_of("spreads", "away", 3.5, 110, 105) == "loss"
    assert G.result_of("spreads", "home", -5.0, 110, 105) == "push"
    assert G.result_of("totals", "over", 220.5, 110, 105) == "loss"
    assert G.result_of("totals", "under", 220.5, 110, 105) == "win"
    assert G.result_of("h2h", "away", None, 110, 105) == "loss"
    assert G.prop_result("over", 20.5, 25) == "win" and G.prop_result("under", 20.0, 20) == "push"


def test_latest_close_pairs_within_a_source_and_takes_the_latest():
    t = TIP - timedelta(minutes=30)
    obs = [(("main", "espn"), t, "home", -5.5, -110), (("main", "espn"), t, "away", 5.5, -110),
           (("poller", "sharpapi"), t + timedelta(minutes=20), "home", -5.0, -115),
           (("poller", "sharpapi"), t + timedelta(minutes=20), "away", 5.0, -105)]
    c = G.latest_close(obs, "home", "spreads", "draftkings")
    assert (c.line, c.price, c.other_price) == (-5.0, -115, -105)
    c = G.latest_close(obs[:1] + [(("main", "espn"), t, "away", 6.0, -110)], "home", "spreads",
                       "draftkings")
    assert (c.line, c.other_price) == (-5.5, None)  # lines do not match: no no-vig pair
    assert G.latest_close(obs, "over", "totals", "draftkings") is None


# --- against databases ----------------------------------------------------------------------------


@pytest.fixture
def pdb(tmp_path):
    return engine_for(tmp_path / "poller.sqlite")


def _game(conn, status="scheduled"):
    sid = sport_id(conn, "nba")
    ids = dict(conn.execute(select(teams.c.abbrev, teams.c.team_id)
                            .where(teams.c.sport_id == sid)).all())  # fmt: skip
    return conn.execute(insert(games).values(
        sport_id=sid, season=2026, season_type="REG", start_time_utc=TIP, home_team_id=ids["BOS"],
        away_team_id=ids["NYK"], status=status)).inserted_primary_key[0]  # fmt: skip


def _ids(conn):
    bk = dict(conn.execute(select(books.c.key, books.c.book_id)).all())
    mk = dict(conn.execute(select(markets.c.key, markets.c.market_id)).all())
    return bk, mk


def _snap(conn, gid, book, market, outcome, line, price, at):
    bk, mk = _ids(conn)
    conn.execute(insert(odds_snapshots).values(
        source="espn_nba", source_game_id=str(gid), game_id=gid, book_id=bk[book],
        market_id=mk[market], outcome=outcome, line=line, price_american=price,
        snapshot_kind="poll", fetched_at=at))  # fmt: skip


def _alert(pdb, gid, **kw):
    vals = dict(signal="sharp", sport="nba", game_id=gid, game_label="NYK @ BOS", tip=TIP,
                market="spreads", side="home", line=-3.5, price=-110, book="draftkings",
                book_prob=0.48, reason="test", dedup_key=f"k{kw}", created_at=TIP - timedelta(
                    hours=3), quiet=False, sent=True)  # fmt: skip
    vals.update(kw)
    with pdb.begin() as p:
        return p.execute(insert(alerts).values(**vals)).inserted_primary_key[0]


def _grade(pdb, aid):
    with pdb.connect() as p:
        return p.execute(select(alert_grades).where(alert_grades.c.alert_id == aid)).first()


def test_spread_alert_graded_idempotently_then_result_arrives(engine, pdb):
    with engine.begin() as c:
        gid = _game(c)
        _snap(c, gid, "draftkings", "spreads", "home", -5.5, -110, TIP - timedelta(minutes=40))
        _snap(c, gid, "draftkings", "spreads", "away", 5.5, -110, TIP - timedelta(minutes=40))
        _snap(c, gid, "draftkings", "spreads", "home", -9.5, -110, TIP + timedelta(minutes=5))
    with pdb.begin() as p:  # the poller's last pre-game poll, later than the main DB's
        p.execute(insert(quotes), [dict(polled_at=TIP - timedelta(minutes=3), sport="nba",
                                        game_id=gid, source="sharpapi", book="draftkings",
                                        market="spreads", outcome=o, line=ln, price=-110)
                                   for o, ln in (("home", -5.0), ("away", 5.0))])  # fmt: skip
    aid = _alert(pdb, gid)
    early = _alert(pdb, gid, tip=NOW + timedelta(hours=5), dedup_key="future")
    assert G.grade_all(engine, pdb, TIP - timedelta(minutes=1)) == 0  # not started
    assert G.grade_all(engine, pdb, NOW) == 1
    g = _grade(pdb, aid)
    assert (g.close_line, g.close_book, g.clv_points, g.result) == (-5.0, "draftkings", 1.5, None)
    assert g.clv_novig == pytest.approx(G.at_bet_line(0.5, 1.5, 12.0) - 0.48, abs=1e-4)
    assert _grade(pdb, early) is None
    assert G.grade_all(engine, pdb, NOW) == 0  # nothing new: nothing rewritten
    with engine.begin() as c:
        c.execute(insert(results).values(game_id=gid, source="espn", home_score=110,
                                         away_score=104, final=True))  # fmt: skip
    assert G.grade_all(engine, pdb, NOW + timedelta(hours=1)) == 1
    g = _grade(pdb, aid)
    assert (g.result, g.profit) == ("win", pytest.approx(0.9091, abs=1e-4))
    assert G.grade_all(engine, pdb, NOW + timedelta(hours=2)) == 0
    with pdb.connect() as p:
        assert len(p.execute(select(alert_grades)).all()) == 1


def test_sharp_alert_falls_back_to_pinnacle_and_cancelled_is_void(engine, pdb):
    with engine.begin() as c:
        gid = _game(c, status="cancelled")
        for o, price in (("over", -120), ("under", 100)):
            _snap(c, gid, "pinnacle", "totals", o, 221.5, price, TIP - timedelta(minutes=10))
    aid = _alert(pdb, gid, market="totals", side="over", line=221.5, price=105, book_prob=0.47)
    G.grade_all(engine, pdb, NOW)
    g = _grade(pdb, aid)
    assert (g.close_book, g.clv_points, g.result, g.profit) == ("pinnacle", 0.0, "void", 0.0)
    assert g.clv_novig == pytest.approx(0.5217 - 0.47, abs=1e-3)
    news = _alert(pdb, gid, signal="news", market="totals", side="over", line=221.5, price=105,
                  dedup_key="news")  # Signal 1: no Pinnacle fallback
    G.grade_all(engine, pdb, NOW)
    assert _grade(pdb, news).close_book is None


def test_nba_prop_alert_close_result_and_did_not_play(engine, pdb):
    with engine.begin() as c:
        gid = _game(c, status="final")
        bk, _ = _ids(c)
        sid = sport_id(c, "nba")
        team = c.execute(select(teams.c.team_id).where(teams.c.sport_id == sid)).scalar()
        c.execute(insert(prop_lines).values(
            source="espn_props", source_game_id="1", game_id=gid, player_id="9",
            player_name="Jayson Tatum", market="points", book_id=bk["draftkings"], kind="poll",
            line=27.5, over_price=-115, under_price=-105,
            fetched_at=TIP - timedelta(minutes=20)))  # fmt: skip
        for pid, name, mins, pts in (("9", "Jayson Tatum", 36.0, 31.0), ("8", "Jrue Holiday",
                                                                         0.0, 0.0)):
            c.execute(insert(nba_player_games).values(
                game_id=gid, team_id=team, player_id=pid, player_name=name, minutes=mins,
                points=pts, source="espn", known_at=NOW, fetched_at=NOW))  # fmt: skip
    over = _alert(pdb, gid, signal="news", market="points", player_name="Tatum, Jayson",
                  side="over", line=25.5, price=-110, book_prob=0.5)
    dnp = _alert(pdb, gid, signal="news", market="points", player_name="Jrue Holiday",
                 side="under", line=12.5, price=-110, book_prob=0.5, dedup_key="dnp")
    G.grade_all(engine, pdb, NOW)
    g = _grade(pdb, over)
    assert (g.close_line, g.clv_points, g.result) == (27.5, 2.0, "win")
    assert g.clv_novig > 0.0
    d = _grade(pdb, dnp)
    assert (d.result, d.profit, d.close_line) == ("void", 0.0, None)


def test_verdict_and_report(pdb, engine):
    s = G.group_stats([{"clv_points": None, "clv_novig": 0.01, "result": "win", "profit": 0.9}]
                      * 199)  # fmt: skip
    assert G.verdict(s).startswith("no verdict before 200")
    rows = [{"clv_points": 1.0, "clv_novig": x, "result": "loss", "profit": -1.0}
            for x in [0.02, 0.01] * 100]
    s = G.group_stats(rows)
    assert s["n_novig"] == 200 and s["lo"] > 0 and G.verdict(s) == "working"
    assert s["roi"] == -1.0
    noisy = [{"clv_points": None, "clv_novig": x, "result": None, "profit": None}
             for x in [0.3, -0.29] * 100]
    assert G.verdict(G.group_stats(noisy)) == "not working"  # mean > 0, range covers zero
    mu, lo, hi = G.mean_range([0.0, 2.0])
    assert (mu, lo, hi) == (1.0, pytest.approx(1 - 1.96), pytest.approx(1 + 1.96))
    with pdb.connect() as p:
        assert G.report_lines(p) == ["no graded alerts yet"]
    with engine.begin() as c:
        gid = _game(c)
        c.execute(insert(results).values(game_id=gid, source="espn", home_score=100,
                                         away_score=99, final=True))  # fmt: skip
    _alert(pdb, gid)
    G.grade_all(engine, pdb, NOW)
    with pdb.connect() as p:
        out = G.report_lines(p)
    assert any(line.startswith("sharp  nba   spreads") for line in out)
    assert "sharp: no verdict before 200 graded (0 so far)" in out


def test_betdata_grade_alerts_cli(engine, monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from betdata.cli import app
    from betdata.config import get_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BETDATA_DB_URL", engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    try:
        out = CliRunner().invoke(app, ["grade", "alerts"])
        assert out.exit_code == 0, out.output
        assert "alert grades written: 0" in out.output and "no graded alerts yet" in out.output
        assert (tmp_path / "data" / "poller.sqlite").exists()
        out = CliRunner().invoke(app, ["alerts", "grade", "--db", str(tmp_path / "p2.sqlite")])
        assert out.exit_code == 0, out.output
    finally:
        get_settings.cache_clear()


def test_nfl_stats_from_the_state_box_table(tmp_path):
    """The poller job has only the hourly state: NFL prop grades read the cached per-game box
    table (`data/derived/nfl_prop_box_<season>.parquet`), mapped to the raw nflverse names."""
    import pandas as pd
    import pytest

    from betdata.alerts.grade import _nfl_stats

    with pytest.raises(FileNotFoundError):
        _nfl_stats(2026, data_dir=tmp_path)
    (tmp_path / "derived").mkdir()
    pd.DataFrame({"nid": ["2026_07_NE_CHI"], "game_id": [7370], "team_id": [1],
                  "player_id": ["00-1"], "name": ["DJ Moore"], "pos": ["WR"],
                  "rec_yds": [87.0], "rush_yds": [4.0], "rec": [6.0]}).to_parquet(
        tmp_path / "derived" / "nfl_prop_box_2026.parquet")
    st = _nfl_stats(2026, data_dir=tmp_path)
    row = st.iloc[0]
    assert (row.game_id, row.player_display_name, row.receiving_yards, row.receptions,
            row.rushing_yards) == ("2026_07_NE_CHI", "DJ Moore", 87.0, 6.0, 4.0)
    # The raw nflverse file wins when it is on disk.
    raw = tmp_path / "nflverse" / "stats_player"
    raw.mkdir(parents=True)
    pd.DataFrame({"player_display_name": ["DJ Moore"], "game_id": ["2026_07_NE_CHI"],
                  "season": [2026], "receiving_yards": [90.0], "rushing_yards": [0.0],
                  "receptions": [7.0]}).to_parquet(raw / "stats_player_week_2026.parquet")
    assert _nfl_stats(2026, data_dir=tmp_path).iloc[0].receiving_yards == 90.0
