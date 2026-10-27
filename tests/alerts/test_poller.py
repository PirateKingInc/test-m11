"""M16 poller (verification c and d): a star's Questionable -> Out is acted on within one cycle,
only that game is re-predicted, exactly one alert is stored and pushed; de-duplication; quiet
hours; Signal 2 re-alerts only after the gap closed and reopened; failures stay contained.

The models and feeds are stubbed here (their real behaviour is tested elsewhere and the NBA path
was replayed on real data, PROJECT.md "M16 replay"); this tests the poller's own logic."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from betdata.alerts import nba
from betdata.alerts.db import alerts, engine_for, poller_runs, repredictions, status_events
from betdata.alerts.feeds import Http, Quote
from betdata.alerts.notify import Alert, emit
from betdata.alerts.poller import Keys, Poller
from tests.nba.synth import synth

NOW = datetime(2021, 10, 20, 18, 0, tzinfo=UTC)  # 2 PM ET, a game day


class Ntfy:
    def __init__(self):
        self.posts = []
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, req):
        self.posts.append((str(req.url), req.headers.get("title"), req.content.decode()))
        return httpx.Response(200)


@pytest.fixture
def setup(engine, tmp_path):
    gids = synth(engine, n_days=3)
    ntfy = Ntfy()
    p = Poller(engine, engine_for(tmp_path / "poller.sqlite"), Keys("sk", None, "topic-x"),
               http=Http(transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                         sleep=lambda s: None),
               now_fn=lambda: NOW, lock=lambda: False, push_client=ntfy.client)  # fmt: skip
    return p, gids, ntfy


@dataclass
class FakeRp:
    model_version: str = "nba15:test"
    point: str = "morning"
    margin_before: float = 3.0
    margin_after: float = -1.0
    total_before: float = 220.0
    total_after: float = 216.0
    props: list = None


class FakeModels:
    calls: list = []

    def store(self, engine, now):
        return None

    def repredict(self, engine, store, change, now):
        FakeModels.calls.append(change.game_id)
        return FakeRp(props=[])

    def p_over(self, market, mean, line):
        return 0.5


def _change(gid, qualifies, name="Star, A", old="Questionable", new="Out",
            report_at=NOW - timedelta(minutes=15)):  # fmt: skip
    return nba.Change(gid, NOW + timedelta(hours=5), "p" + name, name, 1, old, new,
                      0.55 if old else 0.97, 0.0, 34.0 if qualifies else 8.0, qualifies,
                      "Injury-Report_2021-10-20_01_45PM.pdf", report_at, NOW)  # fmt: skip


def _spread_quotes(gid, home_line=-1.5):
    return [Quote("nba", gid, "sharpapi", b, "spreads", side, line, -110)
            for b in ("draftkings", "fanduel")
            for side, line in (("home", home_line), ("away", -home_line))]  # fmt: skip


def test_star_out_one_cycle_one_game_one_alert(setup, monkeypatch):
    p, gids, ntfy = setup
    g1, g2 = gids[0], gids[1]
    rep = nba.Report("Injury-Report_2021-10-20_01_45PM.pdf", NOW - timedelta(minutes=15), NOW)
    monkeypatch.setattr(nba, "active", lambda c, t: True)
    monkeypatch.setattr(nba, "newest_report", lambda http, now, last: (rep.name, b"%PDF"))
    monkeypatch.setattr(nba, "load_new_report", lambda e, n, pdf, now: rep)
    monkeypatch.setattr(nba, "detect", lambda e, s, r, now: [
        _change(g1, True), _change(g2, False, "Bench, B")])
    p._nba_models = FakeModels()
    FakeModels.calls = []
    monkeypatch.setattr(p, "poll_game", lambda sport, gid, now, ev: _spread_quotes(gid))
    p.cycle(NOW)  # one cycle
    with p.pdb.connect() as c:
        ev = c.execute(select(status_events.c.game_id, status_events.c.qualifies)).all()
        assert sorted(ev) == sorted([(g1, True), (g2, False)])
        assert [r.game_id for r in c.execute(select(repredictions))] == [g1]
        al = c.execute(select(alerts)).all()
    assert FakeModels.calls == [g1]  # only the affected game was re-predicted
    # Margin 3.0 -> -1.0 (home), home -1.5: away covers by 2.5 < 3.0 (NBA): no side alert...
    assert al == []
    # ...but at home -4.5 the away side covers by 3.5: exactly one alert, pushed once.
    monkeypatch.setattr(p, "poll_game", lambda sport, gid, now, ev: _spread_quotes(gid, -4.5))
    monkeypatch.setattr(nba, "newest_report", lambda http, now, last: ("next.pdf", b"%PDF"))
    rep2 = nba.Report("next.pdf", NOW, NOW)
    monkeypatch.setattr(nba, "load_new_report", lambda e, n, pdf, now: rep2)
    monkeypatch.setattr(nba, "detect", lambda e, s, r, now: [
        _change(g1, True, name="Star, B", new="Out", old="Doubtful", report_at=NOW)])
    p._due = lambda key, every, now: True
    p.cycle(NOW + timedelta(minutes=3))
    p.cycle(NOW + timedelta(minutes=6))  # the same report again: nothing new
    with p.pdb.connect() as c:
        al = c.execute(select(alerts)).all()
    assert len(al) == 1 and (al[0].side, al[0].line, al[0].sent) == ("away", 4.5, True)
    assert len(ntfy.posts) == 1 and "topic-x" in ntfy.posts[0][0]
    assert "Star" in ntfy.posts[0][2] and "not advice" in ntfy.posts[0][2]


def _alert(gid, key="k1"):
    return Alert("news", "nba", gid, "DEN @ BOS", NOW + timedelta(hours=5), "spreads", "away",
                 4.5, -110, "fanduel", "test", key)  # fmt: skip


def test_dedup_and_quiet_hours(setup):
    p, gids, ntfy = setup
    assert emit(p.pdb, _alert(gids[0]), NOW, "topic-x", ntfy.client) is not None
    assert emit(p.pdb, _alert(gids[0]), NOW, "topic-x", ntfy.client) is None  # duplicate
    night = datetime(2021, 10, 21, 4, 0, tzinfo=UTC)  # midnight ET
    aid = emit(p.pdb, _alert(gids[0], "k2"), night, "topic-x", ntfy.client)
    with p.pdb.connect() as c:
        rows = {r.dedup_key: r for r in c.execute(select(alerts))}
    assert len(ntfy.posts) == 1  # the quiet one was stored, not pushed
    assert rows["k2"].alert_id == aid and rows["k2"].quiet and not rows["k2"].sent
    assert rows["k1"].sent and not rows["k1"].quiet


def test_signal2_realerts_only_after_gap_closes(setup):
    p, gids, ntfy = setup
    g = gids[0]

    def pin(price_home):
        return [Quote("nba", g, "the_odds_api", "pinnacle", "h2h", "home", None, price_home),
                Quote("nba", g, "the_odds_api", "pinnacle", "h2h", "away", None, 115)]

    soft = [Quote("nba", g, "sharpapi", "draftkings", "h2h", "home", None, 105),
            Quote("nba", g, "sharpapi", "draftkings", "h2h", "away", None, -125)]
    assert len(p.signal_sharp("nba", pin(-125), soft, NOW)) == 1
    assert p.signal_sharp("nba", pin(-125), soft, NOW) == []  # still open: no repeat
    assert p.signal_sharp("nba", pin(120), soft, NOW) == []  # gap 2.7 pp: closed
    assert len(p.signal_sharp("nba", pin(-125), soft, NOW)) == 1  # reopened: a new alert
    with p.pdb.connect() as c:
        assert c.execute(select(func.count()).select_from(alerts)).scalar() == 2


def test_a_failing_task_does_not_stop_the_others(setup, monkeypatch):
    p, gids, _ = setup
    monkeypatch.setattr(nba, "active", lambda c, t: True)

    def boom(*a):
        raise RuntimeError("feed down")

    monkeypatch.setattr(p, "nba_report", boom)
    called = []
    monkeypatch.setattr(p, "lag_polls", lambda now: called.append("lag"))
    p.cycle(NOW)
    assert called == ["lag"] and "feed down" in p.errors[0]


def test_run_skips_cycles_while_the_hourly_job_runs(setup, monkeypatch):
    p, gids, _ = setup
    t = [NOW]
    p.now_fn = lambda: t[0]
    p.sleep = lambda s: t.__setitem__(0, t[0] + timedelta(seconds=max(s, 60)))
    p.lock = lambda: t[0] < NOW + timedelta(minutes=2)
    monkeypatch.setattr(p, "has_work", lambda a, b: True)
    monkeypatch.setattr(p, "cycle", lambda now: None)
    p.run(NOW + timedelta(minutes=5))
    with p.pdb.connect() as c:
        r = c.execute(select(poller_runs)).one()
    assert (r.status, r.skipped_cycles, r.cycles) == ("ok", 2, 3)


def test_idle_hour_exits_at_once(setup, monkeypatch):
    p, gids, _ = setup
    monkeypatch.setattr(nba, "active", lambda c, t: False)
    assert p.run(NOW + timedelta(minutes=50)) == 0
    assert p.notes and "idle" in p.notes[0]


def test_a_change_repeated_by_the_next_report_is_not_detected_again(setup, monkeypatch):
    """REVIEW M16-4, live 2026-10-25: VJ Edgecombe Questionable -> Out was detected on the
    6:45 PM report and again on the 7:00 PM report (the next hour's job starts from a copy
    without the 6:45 rows), giving two alerts for one change."""
    p, gids, ntfy = setup
    g1 = gids[0]
    monkeypatch.setattr(nba, "active", lambda c, t: True)
    p._nba_models = FakeModels()
    p._due = lambda key, every, now: True
    monkeypatch.setattr(p, "poll_game", lambda sport, gid, now, ev: _spread_quotes(gid, -4.5))
    for i, (name, old, new) in enumerate([("645PM.pdf", "Questionable", "Out"),
                                          ("700PM.pdf", "Questionable", "Out"),  # the repeat
                                          ("715PM.pdf", "Out", "Questionable"),  # a real flip
                                          ("730PM.pdf", "Questionable", "Out")]):  # and back
        at = NOW + timedelta(minutes=15 * i)
        rep = nba.Report(name, at, at)
        monkeypatch.setattr(nba, "newest_report", lambda http, now, last, n=name: (n, b"%PDF"))
        monkeypatch.setattr(nba, "load_new_report", lambda e, n, pdf, now, r=rep: r)
        monkeypatch.setattr(nba, "detect", lambda e, s, r, now, o=old, n=new, a=at: [
            _change(g1, True, old=o, new=n, report_at=a)])
        p.cycle(at)
    with p.pdb.connect() as c:
        ev = [r.new_status for r in c.execute(select(status_events.c.new_status)
                                              .order_by(status_events.c.event_id))]
    assert ev == ["Out", "Questionable", "Out"]  # the 7:00 PM repeat was skipped
