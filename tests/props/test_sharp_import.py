"""M18 Part E: DK/FD props from the poller's archive into `prop_lines` (no ESPN props), and no
ESPN odds or prop endpoint is called anywhere (verification: no-ESPN test)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import insert, select

from betdata.alerts.db import engine_for, quotes
from betdata.core.espn_odds import EspnOddsDisabled
from betdata.db.schema import prop_lines
from betdata.props.sharp_import import import_props
from tests.nba.synth import synth

T0 = datetime(2021, 10, 20, 15, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def _put(pdb, rows):
    with pdb.begin() as p:
        p.execute(insert(quotes), [dict(sport=s, game_id=g, source="sharpapi", book=b, market=m,
                                        player_name=n, outcome=o, line=ln, price=pr, polled_at=t)
                                   for s, g, b, m, n, o, ln, pr, t in rows])  # fmt: skip


def test_import_pairs_sides_maps_ids_and_is_change_only(engine, tmp_path):
    gid = synth(engine, n_days=2)[0]
    from betdata.db.schema import nba_player_games as pg

    with engine.connect() as c:
        pid, name = c.execute(select(pg.c.player_id, pg.c.player_name)
                              .where(pg.c.game_id == gid).order_by(pg.c.player_id)).first()
    pdb = engine_for(tmp_path / "poller.sqlite")
    t1, t2, t3 = T0, T0 + timedelta(minutes=10), T0 + timedelta(hours=1)
    _put(pdb, [
        ("nba", gid, "draftkings", "rebounds", name, "over", 6.5, -115, t1),
        ("nba", gid, "draftkings", "rebounds", name, "under", 6.5, -105, t1),
        ("nba", gid, "draftkings", "rebounds", "Unknown Guy", "over", 3.5, -110, t1),
        ("nba", gid, "draftkings", "rebounds", "Unknown Guy", "under", 3.5, -110, t1),
        # the over moved alone at t2 (change-only sweep): paired with the under carried forward
        ("nba", gid, "draftkings", "rebounds", name, "over", 6.5, -125, t2),
        # a new line: only the over so far -> no row until the under arrives
        ("nba", gid, "draftkings", "rebounds", name, "over", 7.5, 100, t3),
    ])
    with engine.begin() as m, pdb.connect() as p:
        assert import_props(m, p, T0 + timedelta(hours=2)) == 3
        assert import_props(m, p, T0 + timedelta(hours=2)) == 0  # nothing new
    _put(pdb, [("nba", gid, "draftkings", "rebounds", name, "under", 7.5, -130,
                T0 + timedelta(hours=1, minutes=5))])  # fmt: skip
    with engine.begin() as m, pdb.connect() as p:
        assert import_props(m, p, T0 + timedelta(hours=2)) == 1
        rows = m.execute(select(prop_lines).order_by(prop_lines.c.fetched_at,
                                                     prop_lines.c.player_id)).all()
    got = [(r.player_id, r.line, r.over_price, r.under_price, r.source, r.kind) for r in rows]
    assert got == [(pid, 6.5, -115, -105, "sharpapi", "poll"),  # the name -> its ESPN id
                   ("name:unknown guy", 3.5, -110, -110, "sharpapi", "poll"),
                   (pid, 6.5, -125, -105, "sharpapi", "poll"),
                   (pid, 7.5, 100, -130, "sharpapi", "poll")]
    assert all(r.game_id == gid and r.market == "rebounds" for r in rows)


def test_a_pulled_side_stops_pairing(engine, tmp_path):
    gid = synth(engine, n_days=2)[0]
    pdb = engine_for(tmp_path / "poller.sqlite")
    t1 = T0
    _put(pdb, [
        ("nba", gid, "fanduel", "points", "Some One", "over", 20.5, -110, t1),
        ("nba", gid, "fanduel", "points", "Some One", "under", 20.5, -110, t1),
        ("nba", gid, "fanduel", "points", "Some One", "under", 20.5, None, t1 + timedelta(1 / 24)),
        ("nba", gid, "fanduel", "points", "Some One", "over", 21.5, -105, t1 + timedelta(1 / 12)),
    ])
    with engine.begin() as m, pdb.connect() as p:
        assert import_props(m, p, T0 + timedelta(hours=3)) == 1  # 21.5 has no under: no row


def test_no_poller_database_is_a_no_op(engine):
    with engine.begin() as m:
        assert import_props(m, None, T0) == 0


def test_no_espn_odds_or_prop_job_is_scheduled():
    cfg = yaml.safe_load((ROOT / "config" / "sports.yaml").read_text())
    jobs = [(j["source"], j["job"]) for s in cfg["sports"] for j in s.get("jobs", [])]
    assert not [j for j in jobs if j[0] == "espn_props"]
    assert ("espn_nba", "odds_history") not in jobs
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = wf.read_text()
        assert "espn_props" not in text and "odds_history" not in text, wf.name


def test_espn_odds_paths_refuse(engine):
    from betdata.config import load_settings
    from betdata.nfl.engine.web import fetch_lines
    from betdata.sources import espn_nba, espn_props

    settings = load_settings()
    for mod, cls, job, params in ((espn_props, espn_props.EspnPropsSource, "lines",
                                   {"sport": "nba"}),
                                  (espn_nba, espn_nba.EspnNbaSource, "odds_history",
                                   {"season": 2025})):  # fmt: skip
        src = cls(engine, settings.source(mod.NAME))
        with pytest.raises(EspnOddsDisabled):
            src.fetch(job, params, priority="normal", run_id=0)
        src.close()
    with pytest.raises(EspnOddsDisabled):
        fetch_lines(None, 2025, Path("."))


def test_scoreboard_odds_are_not_stored():
    from betdata.sources.espn_nba import parse_scoreboard

    body = (ROOT / "tests" / "fixtures" / "espn_nba" / "scoreboard_20261021.json").read_bytes()
    assert b'"odds"' in body  # the fixture carries ESPN's embedded lines
    out = parse_scoreboard(body, datetime.now(UTC))
    assert out.events and out.prices == [] and out.requested_markets == ()
