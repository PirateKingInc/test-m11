"""M15 prop line history: export one sport-season to gzipped CSV and load it into a database
that has only the schedule (NFL games matched by kickoff and teams, ESPN id recorded)."""

from datetime import UTC, datetime

from sqlalchemy import delete, select

from betdata.db.schema import game_source_ids, prop_lines
from betdata.props.history import export, load
from tests.conftest import fixture_bytes
from tests.integration.test_espn_props import (  # noqa: F401
    _game,
    espn_odds_allowed,
    props,
    stored,
)


def vals(engine):
    return {k: (r["line"], r["over_price"], r["under_price"]) for k, r in stored(engine)[0].items()}


def test_export_then_load(props, engine, fake_api, clock, tmp_path,  # noqa: F811
                          espn_odds_allowed):  # noqa: F811
    clock.now = datetime(2026, 10, 30, tzinfo=UTC)
    gid = _game(engine, "nfl", "Kansas City Chiefs", "Washington Commanders",
                datetime(2026, 10, 25, 17, 0, tzinfo=UTC), status="final")  # fmt: skip
    fake_api.add(body=fixture_bytes("espn_props", "scoreboard_nfl_20261025.json"))
    fake_api.add(body=fixture_bytes("espn_props", "providers_nfl.json"))
    fake_api.add(body=fixture_bytes("espn_props", "props_nfl.json"))
    assert props.run("history", sport="nfl", season=2026).status == "ok"
    before = vals(engine)
    assert before[("3054850", "rec_yds", "draftkings", "open")] == (14.5, None, None)
    assert before[("3054850", "anytime_td", "draftkings", "close")] == (None, 320, None)
    assert export(engine, tmp_path, "nfl", 2026) == 4

    with engine.begin() as conn:  # a database with the schedule only
        conn.execute(delete(prop_lines))
        conn.execute(delete(game_source_ids).where(game_source_ids.c.source == "espn_nfl"))
    assert load(engine, tmp_path) == {"props_nfl_2026.csv.gz": 4}
    assert load(engine, tmp_path) == {"props_nfl_2026.csv.gz": 0}  # idempotent
    by, rows = stored(engine)
    assert vals(engine) == before
    assert {r["game_id"] for r in rows} == {gid}
    with engine.connect() as conn:
        assert conn.execute(select(game_source_ids.c.game_id).where(
            game_source_ids.c.source == "espn_nfl")).scalars().all() == [gid]
