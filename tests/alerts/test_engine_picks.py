"""M20 Part E in the poller: engine picks are recorded as research signal `nba-engine`, one per
market per game/player, never pushed, never labelled for the card."""

import json
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from betdata.alerts.db import alerts, engine_for
from betdata.alerts.feeds import Http, Quote
from betdata.alerts.gates import signal_name
from betdata.alerts.labels import SIGNALS
from betdata.alerts.poller import Keys, Poller
from betdata.nba.engine import live as L
from tests.nba.synth import synth

NOW = datetime(2021, 10, 20, 18, 0, tzinfo=UTC)
DESIGN = {"w": {"spreads": 0.5, "totals": 0.5, "h2h": 0.5, "points": 0.3},
          "platt": {}, "theta": {"game": 0.03, "prop": 0.05}, "validated": {}}  # fmt: skip


def _game(gid):
    # an engine that likes the home side a lot: margin +8 around a point mass
    p = [0.0] * 141
    p[70 + 8] = 1.0
    return {"game_id": gid, "label": "X @ Y", "tip": NOW.isoformat(), "mu_margin": 8.0,
            "mu_total": 220.0, "p_home_win": 0.9, "margin": {"lo": -70, "p": p},
            "total": {"lo": 140, "p": [1 / 181] * 181}, "checks": {}, "players": []}


def test_engine_pick_recorded_once_never_pushed(engine, tmp_path, monkeypatch):
    gid = synth(engine, n_days=2)[0]
    fit = tmp_path / "fit.json"
    fit.write_text(json.dumps({"fit": {}, "design": DESIGN}))
    monkeypatch.setattr(L, "FIT_PATH", fit)
    monkeypatch.setattr(L, "load_fit", lambda path=fit: (None, json.loads(fit.read_text())))
    p = Poller(engine, engine_for(tmp_path / "poller.sqlite"), Keys("sk", None, "topic"),
               http=Http(transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                         sleep=lambda s: None), now_fn=lambda: NOW, lock=lambda: False)
    monkeypatch.setattr(p, "engine_game", lambda g, now, resim: _game(g))
    qs = [Quote("nba", gid, "sharpapi", "draftkings", "spreads", "home", -3.5, -110),
          Quote("nba", gid, "sharpapi", "draftkings", "spreads", "away", 3.5, -110)]
    out = p.engine_picks(gid, qs, NOW, "hourly")
    assert len(out) == 1
    again = p.engine_picks(gid, qs, NOW + timedelta(minutes=5), "hourly")
    assert again == []  # one pick per market per game
    with p.pdb.connect() as c:
        a = c.execute(select(alerts)).one()
    assert a.signal == "engine" and a.side == "home" and a.sent is False and a.study
    assert signal_name(a.signal, a.sport) == "nba-engine"
    assert "engine" not in SIGNALS  # never labelled, carded or on the sport tabs' tracker
