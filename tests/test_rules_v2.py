"""Rules v2 (owner decision 2026-11-09, betdata/eligibility.py, betdata/calibration.py):
card eligibility, filter reasons, the records' marker, the Filters table, and the parlay's
weekly calibration."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select

from betdata import calibration as CAL
from betdata import eligibility as EL
from betdata.alerts import card as CD
from betdata.alerts.db import alerts, engine_for, quotes

NOW = datetime(2026, 11, 10, 11, 15, tzinfo=UTC)  # Tue Nov 10, 6:15 AM ET
TIP = datetime(2026, 11, 11, 0, 30, tzinfo=UTC)


# --- the rules ---


@pytest.mark.parametrize("card,market,side,line,tm,want", [
    ("og", "assists", "over", 6.5, 0, None),  # (a) assists: eligible
    ("og", "rebounds", "under", 8.5, 0, EL.REBOUNDS),  # (a) no rebounds
    ("og", "rebounds", "over", 8.5, 0, EL.REBOUNDS),
    ("og", "points", "over", 20.5, 0, EL.NON_ASSIST),
    ("og", "spreads", "home", -3.5, None, EL.NOT_ASSISTS_OG),  # OG: assists only
    ("og", "assists", "under", 5.5, 0, EL.UNDER_ASSISTS),  # (b) 5.5 or higher
    ("og", "assists", "under", 7.5, 0, EL.UNDER_ASSISTS),
    ("og", "assists", "under", 4.5, 0, None),
    ("og", "assists", "under", 4.5, 2, EL.TEAMMATES),  # (c) 2+ teammates out
    ("og", "assists", "under", 4.5, 1, None),
    ("og", "assists", "over", 4.5, 3, None),  # (c) unders only
    ("og", "assists", "under", 4.5, None, None),  # unknown: the rule cannot apply
    ("nextgen", "points", "over", 20.5, 0, EL.NON_ASSIST),  # (e)
    ("nextgen", "threes", "over", 2.5, 0, EL.NON_ASSIST),
    ("nextgen", "pra", "under", 30.5, 0, EL.NON_ASSIST),
    ("nextgen", "rebounds", "over", 8.5, 0, EL.REBOUNDS),
    ("nextgen", "assists", "under", 6.5, 0, EL.UNDER_ASSISTS),  # (b) applies too
    ("nextgen", "assists", "under", 3.5, 2, EL.TEAMMATES),  # (c) applies too
    ("nextgen", "assists", "over", 6.5, 3, None),
    ("nextgen", "spreads", "away", 3.5, None, None),  # (f) game markets stay eligible
    ("nextgen", "totals", "under", 221.5, None, None),
    ("nextgen", "h2h", "home", None, None, None),
])  # fmt: skip
def test_rules(card, market, side, line, tm, want):
    assert EL.reason(card, market, side, line, tm) == want


def test_only_games_from_nov_10():
    assert EL.applies(TIP) and not EL.applies(datetime(2026, 11, 10, 3, 0, tzinfo=UTC))
    a = SimpleNamespace(signal="snipe", sport="nba", tip=datetime(2026, 11, 10, 3, tzinfo=UTC),
                        market="rebounds", side="under", line=8.5, player_name="X",
                        details={}, game_id=1, created_at=NOW)  # fmt: skip
    assert EL.alert_reason(None, a) is None  # a Nov 9 game: rules v1
    a.tip = TIP
    assert EL.alert_reason(None, a) == EL.REBOUNDS
    a.signal = "g2"
    assert EL.alert_reason(None, a) is None  # not a card signal


def test_teammates_out_counts_key_teammates_out_or_doubtful(engine):
    from betdata.core.matching import sport_id
    from betdata.db.schema import games, nba_injury_reports, nba_player_games, teams

    with engine.begin() as c:
        sid = sport_id(c, "nba")
        ids = dict(c.execute(select(teams.c.abbrev, teams.c.team_id)
                             .where(teams.c.sport_id == sid)).all())  # fmt: skip
        mia = ids["MIA"]
        past = []
        for i in range(3):  # three past games: key players by minutes
            t = TIP - timedelta(days=3 + i)
            past.append(c.execute(insert(games).values(
                sport_id=sid, season=2026, season_type="REG", start_time_utc=t,
                home_team_id=mia, away_team_id=ids["ORL"],
                status="final")).inserted_primary_key[0])  # fmt: skip
        gid = c.execute(insert(games).values(
            sport_id=sid, season=2026, season_type="REG", start_time_utc=TIP,
            home_team_id=mia, away_team_id=ids["ORL"],
            status="scheduled")).inserted_primary_key[0]  # fmt: skip
        for g in past:
            for pid, name, mins in (("1", "Pick Guy", 34), ("2", "Star One", 35),
                                    ("3", "Star Two", 30), ("4", "Star Three", 28),
                                    ("5", "Bench Guy", 10)):  # fmt: skip
                c.execute(insert(nba_player_games).values(
                    game_id=g, team_id=mia, player_id=pid, player_name=name, minutes=mins,
                    source="espn", known_at=NOW - timedelta(days=1),
                    fetched_at=NOW - timedelta(days=1)))  # fmt: skip

        def report(name, status, known):
            c.execute(insert(nba_injury_reports).values(
                game_date="11/10/2026", matchup="ORL@MIA", game_id=gid, team_id=mia,
                team_name="Miami Heat", player_name=name, status=status, report_at=known,
                known_at=known, source="nba_injury_report", fetched_at=known))  # fmt: skip

        report("One, Star", "Out", NOW - timedelta(hours=2))
        report("Guy, Bench", "Out", NOW - timedelta(hours=2))  # not a key player
        report("Two, Star", "Doubtful", NOW - timedelta(minutes=30))
        report("Three, Star", "Out", NOW + timedelta(hours=1))  # after the pick: not known
    from betdata import review

    review._CACHE.clear()
    with engine.connect() as m:
        assert EL.teammates_out(m, gid, mia, "Pick Guy", NOW) == 2
        assert EL.teammates_out(m, gid, mia, "Pick Guy", NOW - timedelta(hours=1)) == 1
        assert EL.teammates_out(m, gid, mia, "Star One", NOW) == 1  # never counts himself


# --- the card ---


@pytest.fixture
def pdb(tmp_path):
    return engine_for(tmp_path / "poller.sqlite")


class _Main:
    def connect(self):
        import contextlib

        return contextlib.nullcontext(None)


def tip_fn(_m, sport, day):
    return TIP if sport == "nba" else None


def _pick(p, key, signal, market, side, line, fair=0.62, price=110, player=None):
    d = ({"edge": 0.15, "edge_source": "news"} if signal != "engine" else
         {"ev": fair * (1 + price / 100) - 1, "theta": 0.10, "p_market": fair - 0.12})
    p.execute(insert(alerts).values(
        signal=signal, sport="nba", game_id=7, game_label="NYK @ BOS", tip=TIP, market=market,
        player_name=player or key, side=side, line=line, price=price, book="draftkings",
        fair_prob=fair, book_prob=0.45, reason="r", dedup_key=key,
        created_at=NOW - timedelta(hours=2), quiet=True, sent=False, details=d))  # fmt: skip
    p.execute(insert(quotes).values(
        polled_at=NOW - timedelta(minutes=5), sport="nba", game_id=7, source="sharpapi",
        book="draftkings", market=market, player_name=player or key, outcome=side, line=line,
        price=price))  # fmt: skip


def test_og_card_eligible_picks_only_and_reasons_stored(pdb, monkeypatch):
    monkeypatch.setattr(CD, "lines_exist", lambda *a: True)
    monkeypatch.setattr(EL, "teammates_out", lambda m, g, t, pl, at: 2 if pl == "tm" else 0)
    with pdb.begin() as p:
        _pick(p, "ast", "snipe", "assists", "over", 6.5)
        _pick(p, "reb", "snipe", "rebounds", "under", 8.5, fair=0.70)
        _pick(p, "u55", "value", "assists", "under", 5.5, fair=0.70)
        _pick(p, "tm", "snipe", "assists", "under", 4.5, fair=0.70)
        _pick(p, "u45", "snipe", "assists", "under", 4.5)
    [og] = CD.run_shadow(pdb, _Main(), NOW, tip_fn)
    assert sorted(x["dedup_key"] for x in og["picks"]) == ["ast", "u45"]
    assert og["filtered"] == {"reb": EL.REBOUNDS, "u55": EL.UNDER_ASSISTS, "tm": EL.TEAMMATES}


def test_next_gen_card_assists_and_game_markets(pdb, monkeypatch):
    monkeypatch.setattr(CD, "lines_exist", lambda *a: True)
    monkeypatch.setattr(EL, "teammates_out", lambda *a: 0)
    with pdb.begin() as p:
        _pick(p, "pts", "engine", "points", "over", 20.5, fair=0.66)
        _pick(p, "ast", "engine", "assists", "over", 5.5, fair=0.66)
        _pick(p, "spr", "engine", "spreads", "home", -1.5, fair=0.66)
    [card] = CD.run(pdb, _Main(), NOW, tip_fn)
    assert sorted(x["dedup_key"] for x in card["picks"]) == ["ast", "spr"]
    assert card["filtered"] == {"pts": EL.NON_ASSIST}


def test_ledger_stores_every_reason_once_and_still_grades(pdb, tmp_path, monkeypatch):
    from betdata import ledger as L

    monkeypatch.setattr(EL, "teammates_out", lambda *a: 0)
    with pdb.begin() as p:
        _pick(p, "reb", "snipe", "rebounds", "under", 8.5)
        _pick(p, "ast", "engine", "assists", "over", 5.5)
        _pick(p, "pts", "engine", "points", "over", 20.5)
    out = tmp_path / "ledger"
    with pdb.connect() as p:
        L.sync(p, None, out, NOW)
        L.sync(p, None, out, NOW + timedelta(hours=1))
    notes = {(n["pick_id"], n["value"]) for n in L.read_rows(out / "annotations.csv")
             if n["field"] == "filter"}  # fmt: skip
    assert notes == {(L.pick_id("reb"), EL.REBOUNDS), (L.pick_id("pts"), EL.NON_ASSIST)}
    # every candidate is still in the ledger (created, stored and graded as before)
    assert {r["pick_id"] for r in L.read_rows(out / "picks.csv")} == {
        L.pick_id(k) for k in ("reb", "ast", "pts")}
    from betdata import official as O

    rows = {r["pick_id"]: r for r in O.dataset(out)}
    assert rows[L.pick_id("reb")]["filter"] == EL.REBOUNDS


# --- the Filters table and the records' marker ---


def _row(pid, sig, day, res, profit, odds="-110", filt=None, card=False, stake="0.5"):
    return {"pick_id": pid, "sport": "nba", "signal_name": sig, "day": day, "result": res,
            "profit": profit, "odds": odds, "filter": filt or "", "official": card,
            "card_stake": stake if card else "", "clv_consensus": "", "shadow": False}


def test_filters_table_and_review_rule():
    rows = [_row("a", "F", "2026-11-10", "win", "0.909", filt=EL.REBOUNDS),
            _row("b", "F", "2026-11-10", "loss", "-1", filt=EL.REBOUNDS),
            _row("c", "F", "2026-11-11", "loss", "-1", filt=EL.REBOUNDS),
            _row("d", "nba-engine", "2026-11-10", "win", "1.2", odds="120",
                 filt=EL.NON_ASSIST),
            _row("e", "F", "2026-11-09", "win", "1", filt=EL.REBOUNDS),  # before rules v2
            _row("f", "nba-engine", "2026-11-10", "win", "0.909", card=True, stake="1")]
    t = {(r["card"], r["reason"]): r for r in EL.filters_table(rows)}
    og = t[("OG", EL.REBOUNDS)]
    assert (og["removed"], og["wlp"], og["units"]) == (3, "1-2-0", -1.09)
    assert og["win"] == pytest.approx(1 / 3, abs=1e-4) and og["needed"] == pytest.approx(
        1 / 1.909, abs=1e-3)
    assert og["status"] == "review at 100 (3 so far)"
    ng = t[("Next Gen", EL.NON_ASSIST)]
    assert ng["wlp"] == "1-0-0" and (ng["card_wlp"], ng["card_units"]) == ("1-0-0", 0.91)
    many = [_row(f"x{i}", "F", "2026-11-10", "win", "0.9", filt=EL.REBOUNDS)
            for i in range(100)]  # fmt: skip
    [r] = EL.filters_table(many)
    assert r["status"] == "review now: recommend dropping"  # profitable: recommend dropping


def test_record_marker_and_since_rules_v2(tmp_path):
    from betdata.page.official import V2_LABEL, V2_MARKER, card_header

    rows = [_row("old", "nba-engine", "2026-11-08", "win", "1", card=True, stake="1"),
            _row("new", "nba-engine", "2026-11-10", "loss", "-1", card=True, stake="1")]
    for r in rows:
        r.update(game="X @ Y", tip=f"{r['day']}T23:00:00+00:00", created_at="",
                 player="", market="h2h", side="home", line="", book="draftkings")
    html = card_header(rows, "nba", "NBA", date(2026, 11, 11), tmp_path, "nextgen", "Next Gen")
    assert f"<b>{V2_MARKER}</b>" in html and f"<dt>{V2_LABEL}</dt>" in html
    assert "Next Gen 1-1-0 · +0.00u · rules v2 0-1-0 ·" in html  # beside the full record
    assert "goal 2 of 500" in html  # the goal continues on the full record
    i = html.index(f"<dt>{V2_LABEL}</dt>")
    assert "<b>0-1-0</b>" in html[i:i + 300]


# --- the weekly calibration ---


def test_platt_fit_recovers_a_known_shrinkage():
    import math
    import random

    rnd = random.Random(1)
    ps, ys = [], []
    for _ in range(4000):
        p = rnd.uniform(0.05, 0.95)
        x = math.log(p / (1 - p))
        q = 1 / (1 + math.exp(-(0.5 * x - 0.1)))  # the truth: slope 0.5, intercept -0.1
        ps.append(p)
        ys.append(int(rnd.random() < q))
    a, b = CAL.fit(ps, ys)
    assert a == pytest.approx(0.5, abs=0.08) and b == pytest.approx(-0.1, abs=0.08)


def _grades(out, n, shift=0.0):
    from betdata.ledger import append_rows
    from betdata.nba.engine_grades import FIELDS, FILE

    rows = [{"key": f"k{shift}{i}", "p_engine": 0.8 if i % 2 else 0.3,
             "y": "1" if (i % 3) else "0"} for i in range(n)]  # fmt: skip
    append_rows(out / FILE, FIELDS, rows, key="key")


def test_refit_on_mondays_only_once_a_fit_exists(tmp_path):
    out = tmp_path
    _grades(out, 200)
    mon = datetime(2026, 11, 9, 21, 0, tzinfo=UTC)  # Mon Nov 9, 4 PM ET
    assert CAL.maybe_refit(out, mon).fit_date == "2026-11-09"
    assert CAL.maybe_refit(out, mon + timedelta(hours=1)) is None  # once a day
    assert CAL.maybe_refit(out, mon + timedelta(days=1)) is None  # Tuesday: no
    _grades(out, 100, shift=1)
    nxt = CAL.maybe_refit(out, mon + timedelta(days=7))  # next Monday: refit
    assert nxt.fit_date == "2026-11-16" and nxt.n == 300  # only the grades there then
    assert CAL.current(out, date(2026, 11, 12)).fit_date == "2026-11-09"
    assert CAL.current(out, date(2026, 11, 20)).fit_date == "2026-11-16"
    assert CAL.current(out, date(2026, 11, 8)) is None


def test_first_fit_any_day_and_too_few(tmp_path):
    _grades(tmp_path, 10)
    assert CAL.maybe_refit(tmp_path, datetime(2026, 11, 10, 12, tzinfo=UTC)) is None  # < 50
    _grades(tmp_path, 100, shift=1)
    assert CAL.maybe_refit(tmp_path, datetime(2026, 11, 10, 12, tzinfo=UTC)) is not None


def test_parlay_agreement_on_the_calibrated_probability():
    from betdata import parlay as PL

    shrink = CAL.Fit("2026-11-09", 1545, 0.394, -0.13)  # the first live fit
    c = SimpleNamespace(book="draftkings", price=-120, fair=0.6, p_market=0.55, p_engine=0.80,
                        market="assists", side="over", line=6.5, player="P")
    assert PL.qualifies(c, 0.70)  # raw 80% clears 70%...
    assert not PL.qualifies(c, 0.70, shrink)  # ...calibrated (~60%) does not
    c.p_engine = 0.97
    assert PL.qualifies(c, 0.70, shrink)
    lg = PL.leg_of(c, 1, "A @ B", TIP, "who", shrink)
    assert lg.p_raw == 0.97 and lg.p_cal == pytest.approx(shrink(0.97), abs=1e-4)
    assert PL.raw(lg) == lg.p_cal
    assert "(calibrated" in PL.leg_text({**lg.__dict__})
