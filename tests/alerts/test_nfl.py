"""M16 NFL status poller (`alerts/nfl.py`): kickoff windows, change detection across cycles,
the value test, the inactive-list release rule, and a qualifying change opening a lag window and
reaching Signal 1. ESPN responses are hand-built fixtures in the verified live shape
(tests/fixtures/espn_nfl_status); the frozen models are stubbed (the store injection they use is
tested on the synthetic M12 store)."""

from __future__ import annotations

import json
import types
from datetime import UTC, datetime, timedelta

import httpx
import pandas as pd
import pytest
from sqlalchemy import insert, select

from betdata.alerts import nfl as N
from betdata.alerts.db import engine_for, get_state, repredictions, status_events
from betdata.alerts.feeds import Http
from betdata.alerts.nba import Repredicted
from betdata.alerts.poller import Keys, Poller
from betdata.core.matching import sport_id
from betdata.db.schema import (
    game_source_ids,
    games,
    injuries,
    player_game_snaps,
    roster_weeks,
    teams,
)
from betdata.nfl.features.availability import candidates, play_prob
from betdata.nfl.points import FINAL_REPORT, PREKICK, prekick_time
from betdata.nfl.store import AsOf
from tests.conftest import FIXTURES

KICK = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)  # Sunday 1 pm ET
EID = "401772900"
FIX = FIXTURES / "espn_nfl_status"


def _fix(name: str) -> dict:
    return json.loads((FIX / name).read_text())


class Espn:
    """Fake ESPN (and an empty Kalshi): which summary and rosters are live right now."""

    def __init__(self):
        self.summary = "summary_baseline.json"
        self.rosters = {"2": "roster_buf_early.json", "15": "roster_mia_released.json"}
        self.urls: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        self.urls.append(url)
        if "/summary" in url:
            assert req.url.params["event"] == EID
            return httpx.Response(200, json=_fix(self.summary))
        if "/roster" in url:
            team = url.split("/competitors/")[1].split("/")[0]
            return httpx.Response(200, json=_fix(self.rosters[team]))
        if "/scoreboard" in url:
            return httpx.Response(200, json=_fix("scoreboard_20261025.json"))
        if "kalshi" in url:
            return httpx.Response(200, json={"markets": []})
        return httpx.Response(404)

    def count(self, part: str) -> int:
        return sum(part in u for u in self.urls)


def _db(engine, with_espn_id: bool = True) -> dict:
    with engine.begin() as c:
        sid = sport_id(c, "nfl")
        t = dict(c.execute(select(teams.c.abbrev, teams.c.team_id).where(
            teams.c.sport_id == sid)).all())  # fmt: skip
        buf, mia, nyj = t["BUF"], t["MIA"], t["NYJ"]

        def game(kick, home, away, week, status="scheduled"):
            return c.execute(insert(games).values(
                sport_id=sid, season=2026, season_type="REG", week=week, start_time_utc=kick,
                home_team_id=home, away_team_id=away, status=status)).inserted_primary_key[0]

        gid = game(KICK, buf, mia, 7)
        same = game(KICK, t["NE"], nyj, 7)  # same kickoff window
        late = game(KICK + timedelta(hours=3, minutes=25), t["DEN"], t["KC"], 7)
        if with_espn_id:
            c.execute(insert(game_source_ids).values(source="espn_nfl", source_game_id=EID,
                                                     game_id=gid))  # fmt: skip
        # Four earlier games: Allen 100% of offensive snaps, Shakir 50%, Milano 90% (defense).
        for w in range(3, 7):
            g = game(KICK - timedelta(days=7 * (7 - w)), buf, nyj, w, "final")
            for pfr, name, pos, off, de in (("AlleJo00", "Josh Allen", "QB", 1.0, 0.0),
                                            ("ShakKh00", "Khalil Shakir", "WR", 0.5, 0.0),
                                            ("MilaMa00", "Matt Milano", "LB", 0.0, 0.9)):
                c.execute(insert(player_game_snaps).values(
                    source="nflverse", source_game_id=f"2026_0{w}_NYJ_BUF", game_id=g,
                    season=2026, week=w, team_id=buf, player_name=name, pfr_player_id=pfr,
                    position=pos, offense_pct=off, defense_pct=de))  # fmt: skip
        # What M12 knew: Allen Questionable (limited practice) on the final report.
        c.execute(insert(injuries).values(
            sport_id=sid, team_id=buf, season=2026, week=7, player_name="Josh Allen",
            source_player_id="00-0034857", position="QB", report_status="Questionable",
            practice_status="Limited Participation in Practice", source="nflverse",
            fetched_at=KICK - timedelta(days=2)))  # fmt: skip
        for name, gsis, pos in (("Khalil Shakir", "00-0037000", "WR"),
                                ("Matt Milano", "00-0033000", "LB")):
            c.execute(insert(roster_weeks).values(
                source="nflverse", season=2026, week=6, team_id=buf, gsis_id=gsis,
                player_name=name, position=pos, status="ACT"))  # fmt: skip
        c.execute(insert(roster_weeks).values(
            source="nflverse", season=2026, week=6, team_id=mia, gsis_id="00-0035000",
            player_name="Reserve Guy", position="CB", status="RES"))  # fmt: skip
    return {"gid": gid, "same": same, "late": late, "buf": buf, "mia": mia}


class FakeModels:
    made = 0

    def __init__(self, engine, now):
        FakeModels.made += 1
        self.calls = []

    def repredict(self, engine, g, before, after, now):
        self.calls.append((int(g.game_id), before, after))
        return Repredicted("m13w:test", "final_report", 3.0, -1.5, 47.0, 44.0, [])

    def p_over(self, market, mean, line):
        return 0.5

    def play_probs(self, g, obs, now):
        return None  # no model probabilities: the ESPN mapping stands (as on a model failure)


class KnewModels(FakeModels):
    """M12 already had Allen at this probability, whatever ESPN says (REVIEW M17-0)."""

    prob = {"Questionable": 0.003, "Out": 0.003}

    def play_probs(self, g, obs, now):
        st = {o["name"]: o["status"] for o in obs}.get("Josh Allen", "Questionable")
        return {"00-0034857": self.prob[st]}


class MovedModels(KnewModels):
    prob = {"Questionable": 0.8, "Out": 0.01}


class BrokenModels:
    def __init__(self, engine, now):
        raise FileNotFoundError("m13w fit not cached")


@pytest.fixture
def setup(engine, tmp_path, monkeypatch):
    ids = _db(engine)
    espn = Espn()
    handler = N.NflHandler(models_factory=FakeModels)
    p = Poller(engine, engine_for(tmp_path / "poller.sqlite"), Keys(),
               http=Http(transport=httpx.MockTransport(espn), sleep=lambda s: None),
               lock=lambda: False, nfl_handler=handler)  # fmt: skip
    news = []
    monkeypatch.setattr(p, "signal_value", lambda *a: news.append(a) or [])  # M18 Part C
    return p, handler, espn, ids, news


def _events(p):
    with p.pdb.connect() as c:
        return {(r.player_name, r.new_status): r for r in c.execute(select(status_events))}


# --- windows -----------------------------------------------------------------------------------


def test_window_is_100_to_60_minutes_before_each_kickoff(engine):
    ids = _db(engine)
    h = N.NflHandler()
    with engine.connect() as c:
        assert not h.active(c, KICK - timedelta(minutes=101))
        assert h.active(c, KICK - timedelta(minutes=100))
        assert h.active(c, KICK - timedelta(minutes=61))
        assert not h.active(c, KICK - timedelta(minutes=60))
        on = {r.game_id for r in h.window_games(c, KICK - timedelta(minutes=90))}
        assert on == {ids["gid"], ids["same"]}  # the 4:25 game is not in this window
        late = KICK + timedelta(hours=3, minutes=25)
        assert {r.game_id for r in h.window_games(c, late - timedelta(minutes=100))} == {
            ids["late"]}


def test_polls_every_five_minutes_only_inside_windows(setup):
    p, h, espn, ids, _ = setup
    h.cycle(p, KICK - timedelta(minutes=105))
    assert espn.urls == []
    t0 = KICK - timedelta(minutes=100)
    h.cycle(p, t0)
    h.cycle(p, t0 + timedelta(minutes=1))  # not due yet
    assert espn.count("/summary") == 1  # the other game in the window has no ESPN id
    assert espn.count("/roster") == 0  # inactives are not read before 95 minutes out
    h.cycle(p, t0 + timedelta(minutes=5))  # 95 minutes out: the rosters are read too
    assert espn.count("/summary") == 2 and espn.count("/roster") == 2


def test_scoreboard_finds_a_missing_espn_id(engine, tmp_path):
    _db(engine, with_espn_id=False)
    espn = Espn()
    p = Poller(engine, engine_for(tmp_path / "poller.sqlite"), Keys(),
               http=Http(transport=httpx.MockTransport(espn), sleep=lambda s: None),
               lock=lambda: False)  # fmt: skip
    h = N.NflHandler(models_factory=FakeModels)
    h.cycle(p, KICK - timedelta(minutes=100))
    assert espn.count("/scoreboard") >= 1 and espn.count("/summary") == 1
    with engine.connect() as c:
        assert c.execute(select(game_source_ids.c.source_game_id).where(
            game_source_ids.c.source == "espn_nfl")).scalars().all() == [EID]  # fmt: skip


# --- changes and qualification -------------------------------------------------------------------


def test_baseline_records_only_what_m12_did_not_know(setup):
    p, h, espn, ids, news = setup
    h.cycle(p, KICK - timedelta(minutes=100))
    ev = _events(p)
    # Allen: Questionable, as M12 knew. Reserve Guy: IR, as M12's roster knew (RES). Shakir:
    # Questionable, unknown to M12 (a change, not qualifying: 50% of snaps). Waddle: Out, no
    # snap history.
    assert set(ev) == {("Khalil Shakir", "Questionable"), ("Jaylen Waddle", "Out")}
    assert not any(r.qualifies for r in ev.values())
    assert ev[("Khalil Shakir", "Questionable")].old_status is None
    assert ev[("Khalil Shakir", "Questionable")].weight == 0.5
    assert news == []
    with p.pdb.connect() as c:
        state = get_state(c, f"nfl:status:{ids['gid']}")
    assert state["3918298"]["status"] == "Questionable"


def test_change_across_cycles_qualifies_and_reaches_signal_1(setup):
    p, h, espn, ids, news = setup
    t0 = KICK - timedelta(minutes=100)
    h.cycle(p, t0)
    espn.summary = "summary_out.json"
    h.cycle(p, t0 + timedelta(minutes=5))
    ev = _events(p)
    allen = ev[("Josh Allen", "Out")]
    params = h.params()
    assert allen.old_status == "Questionable" and allen.position == "QB"
    assert allen.prob_before == round(play_prob(params, "fr|Questionable|LP"), 4)
    assert allen.prob_after == round(play_prob(params, "fr|Out|LP"), 4)
    assert allen.qualifies and allen.weight == 1.0
    assert allen.report_at.replace(tzinfo=UTC) == datetime(2026, 10, 25, 15, 31, tzinfo=UTC)
    assert not ev[("Khalil Shakir", "Out")].qualifies  # WR under 60% of snaps
    # Only the game is re-predicted, the lag window opens, and Signal 1 is asked.
    model = h._models
    assert [c[0] for c in model.calls] == [ids["gid"]]
    _, before, after = model.calls[0]
    assert {o["name"]: o["status"] for o in before}["Josh Allen"] == "Questionable"
    assert {o["name"]: o["status"] for o in after}["Josh Allen"] == "Out"
    with p.pdb.connect() as c:
        assert f"nfl:{ids['gid']}" in get_state(c, "lag:open")
        rows = c.execute(select(repredictions)).all()
    assert len(rows) == 1 and rows[0].event_id == allen.event_id
    assert rows[0].margin_before == 3.0 and rows[0].margin_after == -1.5
    [(sport, gid, event_id, key, rp, qs, players, reason, *_)] = news
    assert (sport, gid, event_id) == ("nfl", ids["gid"], allen.event_id)
    assert players["Josh Allen"].team_id == ids["buf"]
    assert players["Josh Allen"].new_status == "Out"
    assert "Josh Allen Questionable -> Out" in reason
    # The same statuses read again: nothing new.
    h.cycle(p, t0 + timedelta(minutes=10))
    assert len(_events(p)) == len(ev) and len(news) == 1


@pytest.mark.parametrize("models, qualifies", [(KnewModels, False), (MovedModels, True)])
def test_qualification_uses_the_models_own_play_probability(setup, models, qualifies):
    p, h, espn, ids, news = setup
    h._models_factory = models
    t0 = KICK - timedelta(minutes=100)
    h.cycle(p, t0)
    espn.summary = "summary_out.json"
    h.cycle(p, t0 + timedelta(minutes=5))
    allen = _events(p)[("Josh Allen", "Out")]
    assert allen.qualifies is qualifies
    assert (allen.prob_before, allen.prob_after) == (models.prob["Questionable"],
                                                     models.prob["Out"])
    assert len(news) == int(qualifies)


def test_inactives_count_only_once_the_list_is_released(setup):
    p, h, espn, ids, news = setup
    t = KICK - timedelta(minutes=90)
    h.cycle(p, t)  # baseline; BUF roster not released (10 active entries)
    assert ("Matt Milano", "Inactive") not in _events(p)
    espn.rosters["2"] = "roster_buf_released.json"
    h.cycle(p, t + timedelta(minutes=5))
    ev = _events(p)
    milano = ev[("Matt Milano", "Inactive")]
    assert milano.old_status is None and milano.position == "LB" and milano.weight == 0.9
    assert milano.qualifies  # LB starter, 90% of snaps, not listed -> pk_out
    assert milano.prob_after == round(play_prob(h.params(), "pk_out"), 4)
    assert milano.report_name == f"espn:{EID}:inactives"
    # Allen (Questionable) is on the released list as active: Questionable -> Active.
    allen = ev[("Josh Allen", "Active")]
    assert allen.prob_after == round(play_prob(h.params(), "pk_act|Questionable"), 4)
    # Shakir: Questionable -> Inactive (not qualifying at 50% of snaps).
    assert not ev[("Khalil Shakir", "Inactive")].qualifies
    assert len(news) == 1


def test_model_failure_still_records_and_opens_the_lag_window(setup):
    p, h, espn, ids, news = setup
    h._models_factory = BrokenModels
    t0 = KICK - timedelta(minutes=100)
    h.cycle(p, t0)
    espn.summary = "summary_out.json"
    p.nfl_handler = h
    p.cycle(t0 + timedelta(minutes=5))
    assert _events(p)[("Josh Allen", "Out")].qualifies
    with p.pdb.connect() as c:
        assert f"nfl:{ids['gid']}" in get_state(c, "lag:open")
        assert c.execute(select(repredictions)).all() == []
    assert news == []
    assert any("nfl models" in e for e in p.errors)


def test_a_failing_feed_never_stops_the_poller(setup):
    p, h, espn, ids, news = setup
    espn.summary = "missing.json"
    p.cycle(KICK - timedelta(minutes=100))
    assert any(e.startswith(f"nfl game {ids['gid']}") for e in p.errors)


# --- buckets and the store ------------------------------------------------------------------------


def test_status_buckets():
    assert N.bucket("Out", "Did Not Participate In Practice") == "fr|Out|DNP"
    assert N.bucket("Out") == "fr|Out|DNP"
    assert N.bucket("Questionable") == "fr|Questionable|LP"
    assert N.bucket("Injured Reserve") == "fr_reserve"
    assert N.bucket(N.INACTIVE) == "pk_out"
    assert N.bucket(N.ACTIVE, report="Doubtful") == "pk_act|Doubtful"
    assert N.bucket(None) == "fr|-|-"


def test_store_injection_changes_only_the_news():
    from tests.nfl.synth import make_store

    store = make_store()
    g = store.games.iloc[-1]
    team = int(g.home)
    t_fr = prekick_time(g.kickoff) - pd.Timedelta(minutes=20)
    a = AsOf(store, t_fr)
    cands = {k: b for k, _, role, b in candidates(a, g, team, FINAL_REPORT) if role > 0.3}
    key = sorted(cands)[0]
    obs = [{"status": "Out", "team": team, "gsis": key, "group": None, "practice": None}]
    s2 = N.with_statuses(store, g, obs, t_fr.to_pydatetime())
    after = {k: b for k, _, _, b in candidates(AsOf(s2, t_fr), g, team, FINAL_REPORT)}
    assert after[key] == "fr|Out|DNP"
    assert {k: v for k, v in after.items() if k != key and k in cands} == {
        k: v for k, v in cands.items() if k != key}
    # Inactive after the game-day roster is in the store: a roster INA row (pk_out).
    t_pk = prekick_time(g.kickoff) + pd.Timedelta(minutes=5)
    s3 = N.with_statuses(store, g, [{**obs[0], "status": N.INACTIVE}], t_pk.to_pydatetime())
    pk = {k: b for k, _, _, b in candidates(AsOf(s3, t_pk), g, team, PREKICK)}
    assert pk[key] == "pk_out"
    # Before it: an Out report row instead (no partial game-day roster is invented).
    s4 = N.with_statuses(store, g, [{**obs[0], "status": N.INACTIVE}], t_fr.to_pydatetime())
    assert len(s4.frame("rosters").df) == len(store.frame("rosters").df)
    fr = {k: b for k, _, _, b in candidates(AsOf(s4, t_fr), g, team, FINAL_REPORT)}
    assert fr[key] == "fr|Out|DNP"


def test_model_play_probs_on_a_real_store():
    """Models.play_probs runs M12's own rules (no stub): a starter reported Out moves."""
    from tests.nfl.synth import make_store

    store = make_store()
    g = store.games.iloc[-1]
    team = int(g.home)
    now = (prekick_time(g.kickoff) - pd.Timedelta(minutes=20)).to_pydatetime()
    m = N.Models.__new__(N.Models)
    m.store = store.with_params(**N.m12_params())
    row = types.SimpleNamespace(game_id=int(g.name))
    before = m.play_probs(row, [], now)
    key = max(before, key=before.get)
    obs = [{"status": "Out", "team": team, "gsis": key, "group": None, "practice": None}]
    after = m.play_probs(row, obs, now)
    assert after[key] < 0.1 < before[key]
    assert {k: v for k, v in after.items() if k != key} == {
        k: v for k, v in before.items() if k != key}


def test_a_player_m12_does_not_use_never_qualifies(setup):
    p, h, espn, ids, news = setup
    h._models_factory = type("NoAllen", (KnewModels,), {"play_probs": lambda *a: {}})
    t0 = KICK - timedelta(minutes=100)
    h.cycle(p, t0)
    espn.summary = "summary_out.json"
    h.cycle(p, t0 + timedelta(minutes=5))
    assert not _events(p)[("Josh Allen", "Out")].qualifies and news == []


def test_friday_window_reads_the_weekends_games(engine):
    """M18 Part C: Fridays 16:00-20:00 ET (final injury reports) cover every game of the next
    four days; outside those hours only the T-100..T-60 windows."""
    ids = _db(engine)
    h = N.NflHandler()
    fri = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)  # Friday 5 PM ET, two days before KICK
    with engine.connect() as c:
        assert {ids["gid"], ids["same"], ids["late"]} <= {r.game_id for r in
                                                          h.window_games(c, fri)}
        assert not h.active(c, fri - timedelta(hours=2))  # 3 PM ET
        assert not h.active(c, fri + timedelta(hours=3))  # 8 PM ET


def test_nfl_lag_decision_after_four_weeks(engine, tmp_path):
    from betdata.alerts import lag as L
    from betdata.alerts.db import get_state

    pdb = engine_for(tmp_path / "p.sqlite")
    start = datetime(2026, 11, 1, 17, 0, tzinfo=UTC)
    with pdb.begin() as p:
        p.execute(insert(status_events).values(
            sport="nfl", game_id=1, player_key="x", player_name="X", qualifies=True,
            detected_at=start, report_at=start))  # fmt: skip
    with pdb.begin() as p, engine.connect() as m:
        assert L.decide_nfl(p, m, start + timedelta(days=27)) is None
        d = L.decide_nfl(p, m, start + timedelta(days=28, minutes=1))
        # no DK/FD lag measured: not shown to be at least 5 minutes -> off
        assert d["nfl_value"].startswith("off") and get_state(p, "nfl_value:off")
