"""News alerts, upgraded (owner decision 2026-11-05, betdata/news): the official report every
minute, early sources only when ahead of it, confirmed/contradicted marking, the "good down to"
price, the lag-study switch, the per-source log."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import insert, select

from betdata.alerts.db import alerts, early_claims, engine_for, set_state
from betdata.core.matching import sport_id
from betdata.db.schema import games, nba_injury_reports, teams
from betdata.news import claims as C
from betdata.news import desk as D
from betdata.news import ledger as NL
from betdata.news import sources as S
from betdata.news.parse import AVAILABLE, OUT, Player, match, status_of

NOW = datetime(2026, 11, 6, 22, 0, tzinfo=UTC)  # Fri 5:00 PM ET
TIP = datetime(2026, 11, 7, 0, 30, tzinfo=UTC)


@pytest.fixture
def pdb(tmp_path):
    return engine_for(tmp_path / "poller.sqlite")


def _game(engine):
    with engine.begin() as c:
        sid = sport_id(c, "nba")
        ids = dict(c.execute(select(teams.c.abbrev, teams.c.team_id)
                             .where(teams.c.sport_id == sid)).all())  # fmt: skip
        gid = c.execute(insert(games).values(
            sport_id=sid, season=2026, season_type="REG", start_time_utc=TIP,
            home_team_id=ids["MIA"], away_team_id=ids["ORL"],
            status="scheduled")).inserted_primary_key[0]  # fmt: skip
    return gid, ids


def _report(engine, gid, team, player, status, known):
    with engine.begin() as c:
        c.execute(insert(nba_injury_reports).values(
            game_date="11/06/2026", matchup="ORL@MIA", game_id=gid, team_id=team,
            team_name="Miami Heat", player_name=player, status=status, report_at=known,
            known_at=known, source="nba_injury_report", fetched_at=known))  # fmt: skip


# --- 1. the official report every minute ------------------------------------------------------


def test_official_report_polled_every_minute_lag_polls_unchanged():
    from betdata.alerts import poller as P

    assert P.NBA_CADENCE == timedelta(minutes=1)
    assert P.NBA_LAG_CADENCE == timedelta(minutes=3)  # the SharpAPI lag windows: unchanged


def test_report_checked_on_each_minute_of_a_window(engine, tmp_path, monkeypatch):
    from betdata.alerts import nba
    from betdata.alerts.feeds import Http
    from betdata.alerts.poller import Keys, Poller

    p = Poller(engine, engine_for(tmp_path / "p.sqlite"), Keys("sk", None, None),
               http=Http(transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                         sleep=lambda s: None), now_fn=lambda: NOW, lock=lambda: False)
    calls = []
    monkeypatch.setattr(nba, "active", lambda c, t: True)
    monkeypatch.setattr(p, "nba_report", lambda now: calls.append(now))
    monkeypatch.setattr(p, "news_desk", lambda: SimpleNamespace(cycle=lambda now: None,
                                                                 stop=lambda: None))
    for k in ("sharp_boards", "closing_polls", "sweep", "card", "tracker", "lag_polls"):
        monkeypatch.setattr(p, k, lambda now: None)
    for i in range(4):
        p.cycle(NOW + timedelta(minutes=i))
    assert len(calls) == 4  # once a minute (was every 3)


# --- parsing: clear status language only ------------------------------------------------------


@pytest.mark.parametrize("text,want", [
    ("Jimmy Butler (ankle) has been ruled out tonight vs. Orlando.", OUT),
    ("Heat's Jimmy Butler won't play tonight.", OUT),
    ("Jimmy Butler is OUT", OUT),
    ("Jimmy Butler is available and will start tonight.", AVAILABLE),
    ("Bam Adebayo is starting tonight.", AVAILABLE),
    ("Jimmy Butler is questionable for tonight.", None),
    ("Jimmy Butler is a game-time decision.", None),
    ("Jimmy Butler expected to play tonight.", None),
    ("Jimmy Butler is unavailable tonight.", None),  # not one of the clear words: ignored
    ("Great win tonight!", None),
])  # fmt: skip
def test_status_language(text, want):
    assert status_of(text) == want


def test_player_matching_full_name_or_unique_last_name_with_team():
    ps = [Player("Jimmy Butler", "MIA", 1, 34), Player("Jalen Suggs", "ORL", 1, 30),
          Player("Jalen Green", "HOU", 2, 32)]
    assert [p.name for p in match("Jimmy Butler has been ruled out", ps)] == ["Jimmy Butler"]
    assert [p.name for p in match("Heat: Butler ruled out", ps)] == ["Jimmy Butler"]
    assert match("Butler ruled out", ps) == []  # a last name alone needs the team
    assert match("Jalen ruled out", ps) == []


# --- 2. only if ahead of the official report --------------------------------------------------


def test_only_ahead_of_the_official_report(engine):
    gid, ids = _game(engine)
    with engine.connect() as m:
        assert C.ahead(m, gid, "Jimmy Butler", OUT, NOW) == (True, None)
        assert C.ahead(m, gid, "Jimmy Butler", AVAILABLE, NOW) == (False, None)  # not listed
    _report(engine, gid, ids["MIA"], "Butler, Jimmy", "Out", NOW - timedelta(minutes=30))
    with engine.connect() as m:
        assert C.ahead(m, gid, "Jimmy Butler", OUT, NOW) == (False, "Out")  # already shown
        assert C.ahead(m, gid, "Jimmy Butler", AVAILABLE, NOW) == (True, "Out")


class _Poller:
    """The poller pieces the news desk uses."""

    def __init__(self, main, pdb):
        self.main, self.pdb, self.notes, self.errors = main, pdb, [], []
        self.keys = SimpleNamespace(ntfy_topic="topic-x")
        self.posts = []
        self.push_client = httpx.Client(transport=httpx.MockTransport(
            lambda r: self.posts.append(r) or httpx.Response(200)))
        self.repriced = []

    def _guard(self, what, fn, *a):
        return fn(*a)

    def _due(self, key, every, now):
        return True

    def engine_override(self, gid, now, me, status):
        self.repriced.append((gid, me["name"], status))
        return {"game_id": gid, "players": [{"name": "Jimmy Butler", "team_id": 1},
                                            {"name": "Tyler Herro", "team_id": 1},
                                            {"name": "Jalen Suggs", "team_id": 2}]}

    def poll_game(self, sport, gid, now, ev):
        return []

    def engine_candidates(self, gid, qs, now, game, design):
        from betdata.nba.engine.picks import Candidate

        def c(player, ev, price=120):
            fair = (1 + ev) / 2.2
            return Candidate("points", "over", 20.5, price, "fanduel", fair, 0.45, "consensus",
                             fair, ev, player, 0.45)  # fmt: skip

        return [c("Tyler Herro", 0.14), c("Jalen Suggs", 0.30), c("Jimmy Butler", 0.5),
                c("Bam Weak", 0.05)]

    def game_label(self, gid):
        return "ORL @ MIA", TIP


@pytest.fixture
def desk(engine, pdb, monkeypatch, tmp_path):
    from betdata.nba.engine import live as L

    gid, ids = _game(engine)
    doc = {"games": [{"game_id": gid, "label": "ORL @ MIA", "tip": TIP.isoformat(),
                      "players": [{"name": "Jimmy Butler", "team_id": 1, "player_id": "9",
                                   "is_home": True, "minutes": 33.0},
                                  {"name": "Tyler Herro", "team_id": 1, "player_id": "8",
                                   "is_home": True, "minutes": 32.0}]}]}  # fmt: skip
    monkeypatch.setattr(L, "read", lambda *a, **k: doc)
    monkeypatch.setattr(L, "load_fit", lambda *a: (None, {"design": {
        "w": {}, "platt": {}, "theta": {"game": 0.05, "prop": 0.10}, "validated": {}}}))
    acct = SimpleNamespace(did="did:plc:stein", handle="thesteinline.bsky.social",
                           group="national", team=None)
    d = D.NewsDesk(_Poller(engine, pdb), stream=SimpleNamespace(drain=lambda: [],
                                                                start=lambda: None,
                                                                stop=lambda: None))
    d.by_did = {acct.did: acct}
    return d, gid, ids


def _post(text, at):
    return S.Post("did:plc:stein", text, at, "at://did:plc:stein/app.bsky.feed.post/1")


def _decide(pdb, median):
    with pdb.begin() as p:
        set_state(p, "signal1:decision", {"decided_at": NOW.isoformat(), "soft_events": 40,
                                          "soft_median_min": median}, NOW)


def test_an_ahead_claim_reprices_teammates_and_alerts_above_the_bar(desk, pdb):
    d, gid, _ = desk
    _decide(pdb, 7.5)  # the lag study allows pushes
    from betdata.nba.engine import live as L

    players, rows = D.rotation(L.read(), NOW, 15)
    posted = NOW - timedelta(minutes=4)
    [cid] = d.on_post(_post("Heat's Jimmy Butler has been ruled out tonight.", posted),
                      players, rows, NOW)
    assert d.poller.repriced == [(gid, "Jimmy Butler", "Out")]
    with pdb.connect() as p:
        claim = p.execute(select(early_claims)).one()
        rows_ = p.execute(select(alerts)).all()
    assert claim.ahead and claim.outcome == "pending" and claim.status == OUT
    # teammates only (not the player himself, not the opponent), and only above the bar
    assert [(a.player_name, a.signal) for a in rows_] == [("Tyler Herro", "flash")]
    a = rows_[0]
    det = a.details
    assert det["good_down_to"] == C.good_down_to(a.fair_prob, 0.10)
    assert det["minutes_since"] == 4.0 and det["unconfirmed"] and det["alerts_on"]
    assert a.reason.splitlines()[0] == "News alert — not a card pick"
    assert "ORL @ MIA 7:30 PM ET: Tyler Herro over 20.5 points +120 at FanDuel" in a.reason
    assert f"Good down to +{det['good_down_to']} · 4 min since the news" in a.reason
    assert "Unconfirmed: per @thesteinline.bsky.social" in a.reason
    assert len(d.poller.posts) == 1 and a.sent and a.send_status == "http 200"
    assert d.poller.posts[0].headers["Title"] == "News alert - not a card pick"
    assert d.poller.posts[0].content.decode().startswith("News alert — not a card pick")
    assert claim.alert_ids == [a.alert_id]


def test_a_claim_the_official_report_already_shows_is_ignored(desk, engine, pdb):
    d, gid, ids = desk
    _decide(pdb, 7.5)
    _report(engine, gid, ids["MIA"], "Butler, Jimmy", "Out", NOW - timedelta(minutes=20))
    from betdata.nba.engine import live as L

    players, rows = D.rotation(L.read(), NOW, 15)
    d.on_post(_post("Jimmy Butler ruled out tonight.", NOW - timedelta(minutes=2)), players,
              rows, NOW)  # fmt: skip
    with pdb.connect() as p:
        claim = p.execute(select(early_claims)).one()
        assert p.execute(select(alerts)).all() == []
    assert not claim.ahead and claim.outcome == "official first"
    assert d.poller.repriced == [] and d.poller.posts == []


# --- 3. the lag-study switch ------------------------------------------------------------------


def test_lag_switch(desk, pdb):
    d, gid, _ = desk
    with pdb.connect() as p:
        assert not D.alerts_on(p)  # before the decision: off (logging only)
    _decide(pdb, 3.2)
    with pdb.connect() as p:
        assert not D.alerts_on(p)  # under 5 minutes: off
    from betdata.nba.engine import live as L

    players, rows = D.rotation(L.read(), NOW, 15)
    d.on_post(_post("Jimmy Butler won't play tonight.", NOW - timedelta(minutes=1)), players,
              rows, NOW)  # fmt: skip
    with pdb.connect() as p:
        a = p.execute(select(alerts)).one()  # logged and recorded...
        assert p.execute(select(early_claims)).one().ahead
    assert d.poller.posts == [] and not a.sent and a.details["alerts_on"] is False  # ...no push
    _decide(pdb, 5.0)
    with pdb.connect() as p:
        assert D.alerts_on(p)  # 5 minutes or more: on


# --- 4. confirmed / contradicted ------------------------------------------------------------


def test_confirmed_and_contradicted_marking(desk, engine, pdb):
    d, gid, ids = desk
    _decide(pdb, 7.5)
    from betdata.nba.engine import live as L

    players, rows = D.rotation(L.read(), NOW, 15)
    t0 = NOW - timedelta(minutes=10)
    d.on_post(_post("Jimmy Butler ruled out tonight.", t0), players, rows, NOW)
    d.on_post(_post("Tyler Herro is available tonight.", t0), players, rows, NOW)
    # Herro was listed out before, so "available" was news; the report then says otherwise
    with pdb.begin() as p:
        p.execute(early_claims.update().where(early_claims.c.player_name == "Tyler Herro")
                  .values(ahead=True, outcome="pending"))  # fmt: skip
    _report(engine, gid, ids["MIA"], "Butler, Jimmy", "Out", NOW + timedelta(minutes=12))
    _report(engine, gid, ids["MIA"], "Herro, Tyler", "Out", NOW + timedelta(minutes=12))
    later = NOW + timedelta(minutes=13)
    d.resolve(later, rows)
    with pdb.connect() as p:
        cs = {c.player_name: c for c in p.execute(select(early_claims))}
        a = p.execute(select(alerts).where(alerts.c.player_name == "Tyler Herro")).first()
    assert cs["Jimmy Butler"].outcome == "confirmed" and cs["Jimmy Butler"].lead_min == 22.0
    assert cs["Tyler Herro"].outcome == "contradicted"
    assert a.details["news_status"] == "confirmed" and not a.details["unconfirmed"]


def test_unconfirmed_at_tip(desk, pdb):
    d, gid, _ = desk
    from betdata.nba.engine import live as L

    players, rows = D.rotation(L.read(), NOW, 15)
    d.on_post(_post("Jimmy Butler ruled out tonight.", NOW), players, rows, NOW)
    d.resolve(TIP + timedelta(minutes=1), rows)
    with pdb.connect() as p:
        assert p.execute(select(early_claims.c.outcome)).scalar() == "unconfirmed"


# --- 5. the "good down to" price -------------------------------------------------------------


def test_good_down_to_is_the_worst_price_that_clears_the_bar():
    def ev(fair, american):
        dec = 1 + american / 100 if american > 0 else 1 + 100 / abs(american)
        return fair * dec - 1

    for fair, theta in ((0.55, 0.10), (0.6, 0.05), (0.45, 0.10), (0.52, 0.03)):
        g = C.good_down_to(fair, theta)
        assert ev(fair, g) >= theta - 1e-9  # it clears the bar...
        worse = -101 if g == 100 else g - 1  # one cent worse for the bettor
        assert ev(fair, worse) < theta  # ...and one cent worse does not
    assert C.good_down_to(0.55, 0.10) == 100  # dec 2.0 exactly
    assert C.good_down_to(0.6, 0.05) == -133  # -134 pays 1.746 < 1.75 needed


# --- sources ------------------------------------------------------------------------------------


def test_jetstream_events_and_stream_thread():
    dids = {"did:plc:stein"}
    ev = {"did": "did:plc:stein", "time_us": 1762466400000000, "kind": "commit",
          "commit": {"operation": "create", "collection": "app.bsky.feed.post", "rkey": "3k",
                     "record": {"text": "Butler is out", "createdAt": "2026-11-06T21:58:00Z"}}}
    post, cur = S.parse_event(json.dumps(ev), dids)
    assert post.text == "Butler is out" and cur == ev["time_us"]
    assert S.parse_event(json.dumps({**ev, "did": "did:plc:other"}), dids)[0] is None
    assert S.parse_event(json.dumps({**ev, "commit": {**ev["commit"], "operation": "delete"}}),
                         dids)[0] is None  # fmt: skip

    class WS:
        def __init__(self, msgs):
            self.msgs = list(msgs)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def recv(self, timeout=None):
            if self.msgs:
                return self.msgs.pop(0)
            raise TimeoutError

    seen = []

    def connect(url, **kw):
        seen.append(url)
        return WS([json.dumps(ev)])

    st = S.Stream("wss://jetstream.example/subscribe", list(dids), connect=connect)
    st.start()
    import time

    for _ in range(50):
        if not st.q.empty():
            break
        time.sleep(0.02)
    st.stop()
    assert [p.text for p in st.drain()] == ["Butler is out"] and not st.running
    assert "wantedDids=did%3Aplc%3Astein" in seen[0]
    assert "wantedCollections=app.bsky.feed.post" in seen[0]


def test_sleeper_spike_is_a_hint_only():
    rows = [{"player_id": "1", "count": 90}, {"player_id": "2", "count": 30},
            {"player_id": "3", "count": 300}]
    prev = {"1": 20, "2": 25}
    assert S.sleeper_spikes(rows, prev, 3.0, 25) == [("1", 90), ("3", 300)]


def test_source_table():
    rows = [
        {"source": "bluesky", "account": "a", "ahead": "true", "outcome": "confirmed",
         "lead_min": "12"},
        {"source": "bluesky", "account": "a", "ahead": "true", "outcome": "confirmed",
         "lead_min": "4"},
        {"source": "bluesky", "account": "a", "ahead": "true", "outcome": "contradicted",
         "lead_min": ""},
        {"source": "bluesky", "account": "a", "ahead": "false", "outcome": "official first",
         "lead_min": ""}]  # fmt: skip
    [t] = NL.source_table(rows)
    assert (t["claims"], t["first"], t["confirmed"], t["contradicted"], t["official_first"],
            t["median_lead"]) == (4, 3, 2, 1, 1, 8.0)


def test_news_alerts_are_their_own_signal_never_on_a_card():
    from datetime import date

    from betdata.alerts.card import signals_for, today_signals
    from betdata.alerts.gates import signal_name

    assert signal_name("flash", "nba") == "news-alert"
    for d_ in (date(2026, 11, 6), date(2026, 11, 3)):
        assert "flash" not in signals_for("nba", d_) and "flash" not in today_signals("nba", d_)


def test_ledger_log_annotations_and_research_section(desk, engine, pdb, tmp_path):
    from betdata import ledger as L
    from betdata import official as O
    from betdata.page.news import news_html

    d, gid, ids = desk
    _decide(pdb, 7.5)
    from betdata.nba.engine import live as L_

    players, rows = D.rotation(L_.read(), NOW, 15)
    d.on_post(_post("Jimmy Butler ruled out tonight.", NOW - timedelta(minutes=9)), players,
              rows, NOW)  # fmt: skip
    _report(engine, gid, ids["MIA"], "Butler, Jimmy", "Out", NOW + timedelta(minutes=6))
    d.resolve(NOW + timedelta(minutes=7), rows)
    out = tmp_path / "ledger"
    with pdb.connect() as p, engine.connect() as m:
        L.sync(p, m, out, NOW + timedelta(minutes=8))
        L.sync(p, m, out, NOW + timedelta(minutes=9))  # append-only: nothing new
    [row] = L.read_rows(out / "news_sources.csv")
    assert (row["account"], row["outcome"], row["lead_min"]) == (
        "thesteinline.bsky.social", "confirmed", "15.0")
    notes = [n for n in L.read_rows(out / "annotations.csv") if n["field"] == "news_status"]
    assert [n["value"] for n in notes] == ["confirmed"]
    [sw] = L.read_rows(out / "news_switch.csv")
    assert sw["alerts"] == "on" and sw["soft_median_min"] == "7.5"
    picks = O.dataset(out)
    assert {r["signal_name"] for r in picks} == {"news-alert"}
    assert not any(r["official"] for r in picks)  # never a card pick
    site = tmp_path / "site"
    html = news_html(picks, site, out)
    assert "<h2>News alerts</h2>" in html and "News alert pushes: ON" in html
    assert "<li class='prop' data-source='thesteinline.bsky.social'>" in html
    assert "First 1 · median lead 15.0 min" in html and "Confirmed 1 · contradicted 0" in html
    assert "<table" not in html and "Sleeper add spikes: 0 logged, 0 corroborated" in html
    assert "EV at close" in html and (site / "csv/news-sources.csv").exists()
