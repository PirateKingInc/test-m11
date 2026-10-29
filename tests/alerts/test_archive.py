"""M18 Part E archive (verification: archive test): openers are written once and never
overwritten; sweep quotes are stored change-only; sweeps run at 06:00, hourly 07-23 and 23:30 ET
and poll every game with a tip in the next 36 hours, a few a cycle."""

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, insert, select

from betdata.alerts.archive import sweep_pending, sweep_slot
from betdata.alerts.db import engine_for, openers, quotes
from betdata.alerts.feeds import Http, Quote
from betdata.alerts.poller import Keys, Poller
from betdata.db.schema import game_source_ids
from tests.nba.synth import synth

NOW = datetime(2021, 10, 20, 11, 5, tzinfo=UTC)  # 7:05 AM ET


def _poller(engine, tmp_path):
    return Poller(engine, engine_for(tmp_path / "poller.sqlite"), Keys("sk", None, None),
                  http=Http(transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                            sleep=lambda s: None),
                  now_fn=lambda: NOW, lock=lambda: False)  # fmt: skip


def _q(gid, line, price, player=None, market="spreads", outcome="home"):
    return Quote("nba", gid, "sharpapi", "draftkings", market, outcome, line, price, player)


def test_openers_written_once_never_overwritten(engine, tmp_path):
    p = _poller(engine, tmp_path)
    gid = synth(engine, n_days=1)[0]
    p.store_quotes([_q(gid, -3.5, -110), _q(gid, 10.5, -115, "LeBron James", "rebounds", "over")],
                   NOW)
    p.store_quotes([_q(gid, -4.5, -105), _q(gid, 11.5, -110, "James, LeBron", "rebounds", "over")],
                   NOW + timedelta(hours=1))  # fmt: skip
    with p.pdb.connect() as c:
        rows = {(r.market, r.player_key): (r.line, r.price, r.first_seen)
                for r in c.execute(select(openers))}
        n_quotes = c.execute(select(func.count()).select_from(quotes)).scalar_one()
    assert rows[("spreads", "")][:2] == (-3.5, -110)
    assert rows[("rebounds", "lebronjames")][:2] == (10.5, -115)  # name forms share one key
    assert len(rows) == 2 and n_quotes == 4  # every poll is still kept in quotes


def test_change_only_storage(engine, tmp_path):
    p = _poller(engine, tmp_path)
    gid = synth(engine, n_days=1)[0]
    for i, line in enumerate((-3.5, -3.5, -4.0, -4.0)):
        p.store_quotes([_q(gid, line, -110)], NOW + timedelta(minutes=i), change_only=True)
    with p.pdb.connect() as c:
        lines = [r.line for r in c.execute(select(quotes).order_by(quotes.c.quote_id))]
    assert lines == [-3.5, -4.0]


def test_sweep_slots():
    et = lambda h, m: datetime(2026, 11, 1, h, m, tzinfo=UTC) + timedelta(hours=5)  # noqa: E731
    assert sweep_slot(et(5, 59)) is None
    assert sweep_slot(et(6, 0)) == "2026-11-01:0600"
    assert sweep_slot(et(7, 59)) == "2026-11-01:0700"
    assert sweep_slot(et(23, 29)) == "2026-11-01:2300"
    assert sweep_slot(et(23, 30)) == "2026-11-01:2330"
    assert sweep_pending("2026-11-01:0700", [], et(7, 10), et(7, 55)) is False
    assert sweep_pending("2026-11-01:0700", [], et(7, 10), et(8, 1)) is True
    assert sweep_pending("2026-11-01:0700", [["nba", 1]], et(7, 10), et(7, 11)) is True


def test_sweep_polls_every_game_in_36_hours_a_few_per_cycle(engine, tmp_path, monkeypatch):
    p = _poller(engine, tmp_path)
    gids = synth(engine, n_days=3)
    with engine.begin() as c:
        c.execute(insert(game_source_ids), [dict(game_id=g, source="sharpapi",
                                                  source_game_id=f"ev{g}") for g in gids])
    polled = []

    def game(conn, sport, ev):
        gid = int(ev[2:])
        polled.append(gid)
        return [_q(gid, -2.5, -110), _q(gid, 7.5, -120, "A Player", "assists", "over")]

    monkeypatch.setattr(p.sharp, "game", game)
    from betdata.db.schema import games

    with engine.connect() as c:
        soon = set(c.execute(select(games.c.game_id).where(
            games.c.game_id.in_(gids), games.c.start_time_utc > NOW,
            games.c.start_time_utc <= NOW + timedelta(hours=36))).scalars())  # fmt: skip
    assert soon and len(soon) < len(gids)
    n = 0
    while True:
        done = p.sweep(NOW + timedelta(minutes=n))
        n += 1
        assert done <= 3
        if not done:
            break
    assert sorted(polled) == sorted(soon)
    p.sweep(NOW + timedelta(minutes=30))  # same slot: nothing new
    assert sorted(polled) == sorted(soon)
    with p.pdb.connect() as c:
        assert c.execute(select(func.count()).select_from(openers)).scalar_one() == 2 * len(soon)
