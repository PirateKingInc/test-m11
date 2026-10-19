"""The latest market per game (shared by predictions and the page)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from betdata.db.schema import game_source_ids
from betdata.nfl.live import attach_moneylines
from betdata.nfl.predict import LivePick
from betdata.quotes import current_quotes
from betdata.sources.nflverse import NflverseSource
from tests.conftest import fixture_bytes


def test_current_quotes_and_moneylines(engine, settings, fake_api, clock):
    nfl = NflverseSource(engine, settings.source("nflverse"), transport=fake_api.transport,
                         sleep=lambda s: None, clock=clock)  # fmt: skip
    fake_api.add(body=fixture_bytes("nflverse", "games_sample.csv"))
    assert nfl.run("schedules", seasons="all").status == "ok"
    with engine.connect() as conn:
        gid = conn.execute(select(game_source_ids.c.game_id).where(
            game_source_ids.c.source_game_id == "2026_06_HOU_JAX")).scalar()  # fmt: skip
        assert current_quotes(conn, [gid], clock.now - timedelta(days=1)) == {}  # not seen yet
        q = current_quotes(conn, [gid], clock.now)[gid]
        # spread_line 2.5 = JAX (home) favoured by 2.5 -> home spread -2.5
        assert q["spread_home"].line == -2.5 and q["total"].line == 42.5
        assert (q["ml_home"].price, q["ml_away"].price) == (-142, 120)
        assert q["ml_home"].source == "nflverse" and q["ml_home"].book == "nflverse"
        assert q["ml_home"].fetched_at == clock.now
        pick = LivePick(gid, datetime(2026, 10, 18, 17, tzinfo=UTC), "JAX", "HOU", "early",
                        1.0, 44.0, 0.53, -2.5, 42.5, None, None)  # fmt: skip
        attach_moneylines(conn, [pick], clock.now)
    assert (pick.ml_home, pick.ml_away) == (-142, 120)
