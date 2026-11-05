"""The NBA engine card (owner override 2026-11-04, MODEL.md "Engine card"): from the 2026-11-05
card, the NBA card is the top 5 engine picks by EV still Live at selection (6:15 AM ET since
2026-11-05; 8:50 before), a short card chosen once, TAKE when EV >= 1.5x the engine's threshold,
else LEAN. NFL is unchanged; the archived NBA signals (C-NBA, F) are out of the pool."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from betdata.alerts import card as CD
from betdata.alerts import labels as LB
from betdata.alerts.db import alerts, engine_for, get_state, quotes
from betdata.alerts.tracker import build

NOW = datetime(2026, 11, 5, 11, 15, tzinfo=UTC)  # Thu Nov 5, 6:15 AM ET (EST)
TIP = datetime(2026, 11, 6, 0, 0, tzinfo=UTC)  # 7:00 PM ET


@pytest.fixture
def pdb(tmp_path):
    return engine_for(tmp_path / "poller.sqlite")


@pytest.fixture(autouse=True)
def _lines_from_the_poller(monkeypatch):
    """No main database here: "lines exist" = any stored quote of the sport by `now`."""
    def lines(p, m, sport, day, now):
        return p.execute(select(quotes.c.quote_id).where(
            quotes.c.sport == sport, quotes.c.polled_at <= now).limit(1)).first() is not None

    monkeypatch.setattr(CD, "lines_exist", lines)


def _pick(p, key, ev, signal="engine", price=110, created=NOW - timedelta(hours=1), live=True,
          player="A Player", theta=0.10, sport="nba"):  # fmt: skip
    fair = (1 + ev) / 2.1  # EV = fair x 2.1 - 1 at +110
    d = {"ev": ev, "theta": theta, "p_engine": fair, "p_market": 0.48}
    if signal != "engine":
        d = {"edge": 0.2, "edge_source": "news"}
    p.execute(insert(alerts).values(
        signal=signal, sport=sport, game_id=7, game_label="NYK @ BOS", tip=TIP,
        market="rebounds", player_name=player, side="over", line=6.5, price=price,
        book="draftkings", fair_prob=fair, book_prob=0.45, reason="r", dedup_key=key,
        created_at=created, quiet=True, sent=False, details=d))  # fmt: skip
    p.execute(insert(quotes).values(
        polled_at=NOW - timedelta(minutes=5), sport=sport, game_id=7, source="sharpapi",
        book="draftkings", market="rebounds", player_name=player, outcome="over", line=6.5,
        price=price if live else price - 60))  # fmt: skip


class _Main:
    def connect(self):
        import contextlib

        return contextlib.nullcontext(None)


def tip_fn(_m, sport, day):
    return TIP if sport == "nba" else None


def test_signals_switch_on_nov_5_for_the_nba_only():
    from datetime import date

    assert CD.signals_for("nba", date(2026, 11, 4)) == LB.SIGNALS
    assert CD.signals_for("nba", date(2026, 11, 5)) == ("engine",)
    assert CD.signals_for("nfl", date(2026, 11, 8)) == LB.SIGNALS


def test_engine_card_top_five_live_engine_picks_only(pdb):
    with pdb.begin() as p:
        for i, ev in enumerate((0.11, 0.16, 0.12, 0.30, 0.14, 0.13, 0.20)):
            _pick(p, f"e{i}", ev, player=f"P{i}")
        _pick(p, "gone", 0.5, live=False, player="Gone")
        _pick(p, "after", 0.5, created=NOW + timedelta(minutes=1), player="After")
        _pick(p, "snipe", 0.9, signal="snipe", player="F")  # archived: never in the pool
        _pick(p, "value", 0.9, signal="value", player="C")
    [card] = CD.run(pdb, _Main(), NOW, tip_fn)
    assert [x["dedup_key"] for x in card["picks"]] == ["e3", "e6", "e1", "e4", "e5"]
    assert card["source"] == "engine" and card["live"] == 7
    # TAKE when EV >= 1.5 x theta (0.15), else LEAN
    assert {x["dedup_key"]: x["label"] for x in card["picks"]} == {
        "e3": "TAKE", "e6": "TAKE", "e1": "TAKE", "e4": "LEAN", "e5": "LEAN"}
    assert {x["dedup_key"]: x["stake"] for x in card["picks"]}["e3"] == 1.0
    assert {x["dedup_key"]: x["stake"] for x in card["picks"]}["e5"] == 0.5


def test_a_short_engine_card_is_chosen_once_and_says_so(pdb):
    with pdb.begin() as p:
        _pick(p, "a", 0.12)
        _pick(p, "b", 0.20, player="B")
    [card] = CD.run(pdb, _Main(), NOW, tip_fn)
    assert len(card["picks"]) == 2
    title, body = CD.message(pdb.connect().__enter__(), card, NOW)
    assert body.splitlines()[-1] == "2 picks today — the engine found no other value"
    with pdb.begin() as p:  # a pick found after selection stays off the card
        _pick(p, "late", 0.5, created=NOW + timedelta(minutes=20), player="Late")
    assert CD.run(pdb, _Main(), NOW + timedelta(minutes=30), tip_fn) == []
    with pdb.connect() as p:
        doc = build(p, None, "nba", NOW.date(), NOW + timedelta(minutes=30))
    assert doc["card_note"] == "2 picks today — the engine found no other value"
    picks = {x["id"]: x for g in doc["games"] for x in g["picks"]}
    assert sum(x["card"] for x in picks.values()) == 2 and len(picks) == 3


def _quote(p, at, player="Q"):
    p.execute(insert(quotes).values(
        polled_at=at, sport="nba", game_id=7, source="sharpapi", book="draftkings",
        market="rebounds", player_name=player, outcome="over", line=6.5, price=110))


def test_case1_lines_at_615_select_now_even_empty(pdb):
    """Lines exist at 6:15 for at least one game: the card is selected then from what is
    available, empty here; a pick found later never joins it."""
    with pdb.begin() as p:
        _pick(p, "gone", 0.3, live=False)  # a line exists, but nothing is Live
    [card] = CD.run(pdb, _Main(), NOW, tip_fn)
    assert card["picks"] == [] and not card.get("no_lines")
    assert CD.card_note(card) == "No picks today — the engine found no value"
    _t, body = CD.message(pdb.connect().__enter__(), card, NOW)
    assert body == "No picks today — the engine found no value"
    with pdb.begin() as p:  # a Live pick later that day: tracked on Research, never carded
        _pick(p, "new", 0.3, created=NOW + timedelta(hours=1), player="New")
    assert CD.run(pdb, _Main(), NOW + timedelta(hours=2), tip_fn) == []
    with pdb.connect() as p:
        assert get_state(p, "card:nba:2026-11-05")["picks"] == []
        doc = build(p, None, "nba", NOW.date(), NOW + timedelta(hours=2))
    assert doc["card_note"] == "No picks today — the engine found no value"
    # the shadow Card v1 follows the same rule: chosen at 6:15, empty
    [sh] = CD.run_shadow(pdb, _Main(), NOW, tip_fn)
    assert sh["picks"] == [] and sh["source"] == CD.SHADOW


def test_case2_no_lines_at_615_wait_for_the_first_poll_with_lines(pdb):
    """No lines for any of today's games at 6:15: wait; the first later poll where lines
    exist selects the card (same rules), once."""
    assert CD.run(pdb, _Main(), NOW, tip_fn) == []
    assert CD.run_shadow(pdb, _Main(), NOW, tip_fn) == []
    with pdb.connect() as p:
        assert get_state(p, "card:nba:2026-11-05") is None
    later = NOW + timedelta(hours=3)
    with pdb.begin() as p:
        _pick(p, "a", 0.12, created=later - timedelta(minutes=5))
        _pick(p, "b", 0.20, created=later - timedelta(minutes=5), player="B")
    [card] = CD.run(pdb, _Main(), later, tip_fn)
    assert [x["dedup_key"] for x in card["picks"]] == ["b", "a"]
    assert card["selected_at"] == later.isoformat()
    assert CD.card_note(card) == "2 picks today — the engine found no other value"
    assert CD.run(pdb, _Main(), later + timedelta(minutes=5), tip_fn) == []  # once


def test_case3_no_lines_60_minutes_before_the_first_tip_publish_empty(pdb):
    """Still no lines 60 minutes before the day's first tip: the card is published empty with
    "No lines posted in time", and the waiting stops."""
    early = TIP - timedelta(minutes=61)
    assert CD.run(pdb, _Main(), early, tip_fn) == []  # still waiting
    cutoff = TIP - timedelta(minutes=60)
    [card] = CD.run(pdb, _Main(), cutoff, tip_fn)
    assert card["picks"] == [] and card["no_lines"] and card["source"] == "engine"
    assert CD.card_note(card) == "No lines posted in time"
    [sh] = CD.run_shadow(pdb, _Main(), cutoff, tip_fn)
    assert sh["no_lines"] and sh["source"] == CD.SHADOW
    with pdb.begin() as p:  # lines arrive later: the card is already chosen, once
        _pick(p, "late", 0.3, created=cutoff + timedelta(minutes=5))
    assert CD.run(pdb, _Main(), cutoff + timedelta(minutes=10), tip_fn) == []
    with pdb.connect() as p:
        doc = build(p, None, "nba", NOW.date(), cutoff + timedelta(minutes=10))
        _t, body = CD.message(p, card, cutoff)
    assert doc["card_note"] == "No lines posted in time" and body == "No lines posted in time"


def test_lines_exist_reads_todays_games(engine, pdb, monkeypatch):
    """The real check: a stored quote for one of the sport's games on the Eastern day."""
    import importlib

    from betdata.core.matching import sport_id
    from betdata.db.schema import games, teams

    monkeypatch.undo()
    real = importlib.reload(CD).lines_exist
    with engine.begin() as c:
        sid = sport_id(c, "nba")
        ids = dict(c.execute(select(teams.c.abbrev, teams.c.team_id)
                             .where(teams.c.sport_id == sid)).all())  # fmt: skip
        gid = c.execute(insert(games).values(
            sport_id=sid, season=2026, season_type="REG", start_time_utc=TIP,
            home_team_id=ids["BOS"], away_team_id=ids["NYK"],
            status="scheduled")).inserted_primary_key[0]  # fmt: skip
    with pdb.connect() as p, engine.connect() as m:
        assert not real(p, m, "nba", NOW.date(), NOW)
    with pdb.begin() as p:
        p.execute(insert(quotes).values(
            polled_at=NOW - timedelta(minutes=8), sport="nba", game_id=gid, source="sharpapi",
            book="draftkings", market="spreads", outcome="home", line=-3.5, price=-110))
    with pdb.connect() as p, engine.connect() as m:
        assert real(p, m, "nba", NOW.date(), NOW)
        assert not real(p, m, "nba", NOW.date(), NOW - timedelta(minutes=10))  # not yet then
        assert not real(p, m, "nfl", NOW.date(), NOW)


def test_engine_label_rule():
    from types import SimpleNamespace

    class P:  # nothing stored: the price is taken as available (priced from this poll)
        def execute(self, *a, **k):
            class R:
                def all(self):
                    return []
            return R()

    def a(ev, theta):
        return SimpleNamespace(signal="engine", details={"ev": ev, "theta": theta}, tip=TIP,
                               game_id=7, market="totals", player_name=None, side="over",
                               line=220.5, price=-110, book="draftkings")

    assert LB.label(P(), a(0.075, 0.05), NOW)["label"] == "TAKE"
    assert LB.label(P(), a(0.074, 0.05), NOW)["label"] == "LEAN"
    assert LB.label(P(), a(0.075, 0.05), NOW)["stake"] == 1.0


# --- the shadow Card v1 (owner request 2026-11-05) -------------------------------------------


def _v1(p, key, fair, signal="value", player="V", created=NOW - timedelta(hours=1), live=True):
    """An F / C-NBA pick, with its edge (C-NBA prop bar 6 pp)."""
    p.execute(insert(alerts).values(
        signal=signal, sport="nba", game_id=7, game_label="NYK @ BOS", tip=TIP,
        market="rebounds", player_name=player, side="over", line=6.5, price=110,
        book="draftkings", fair_prob=fair, book_prob=0.45, reason="r", dedup_key=key,
        created_at=created, quiet=True, sent=False,
        details={"edge": fair - 0.45, "edge_source": "news"}))  # fmt: skip
    p.execute(insert(quotes).values(
        polled_at=NOW - timedelta(minutes=5), sport="nba", game_id=7, source="sharpapi",
        book="draftkings", market="rebounds", player_name=player, outcome="over", line=6.5,
        price=110 if live else 50))  # fmt: skip


def test_shadow_card_v1_is_chosen_beside_the_engine_card_and_never_official(pdb):
    with pdb.begin() as p:
        _pick(p, "e", 0.2)
        for i, fair in enumerate((0.55, 0.60, 0.52, 0.58, 0.57, 0.53)):
            _v1(p, f"v{i}", fair, signal="value" if i % 2 else "snipe", player=f"V{i}")
        _v1(p, "gone", 0.7, player="G", live=False)
    [card] = CD.run(pdb, _Main(), NOW, tip_fn)  # the official card: the engine's, unchanged
    assert [x["dedup_key"] for x in card["picks"]] == ["e"] and card["source"] == "engine"
    [sh] = CD.run_shadow(pdb, _Main(), NOW, tip_fn)
    assert sh["source"] == CD.SHADOW == "card-v1-shadow"
    assert [x["dedup_key"] for x in sh["picks"]] == ["v1", "v3", "v4", "v0", "v5"]
    assert CD.run_shadow(pdb, _Main(), NOW + timedelta(minutes=5), tip_fn) == []  # once
    with pdb.connect() as p:
        assert get_state(p, "shadow:nba:2026-11-05")["picks"] == sh["picks"]
        assert [x["dedup_key"] for x in get_state(p, "card:nba:2026-11-05")["picks"]] == ["e"]
        doc = build(p, None, "nba", NOW.date(), NOW)
    # owner 2026-11-05: OG is official too, so Today lists both pools; each pick says which
    # card it is on (the toggle picks the card)
    picks = {x["id"]: x for g in doc["games"] for x in g["picks"]}
    assert {x["signal"] for x in picks.values()} == {"nba-engine", "C-NBA", "F"}
    assert sum(x["card"] for x in picks.values()) == 1
    assert sum(x["og"] for x in picks.values()) == 5
    assert {x["og_label"] for x in picks.values() if x["og"]} <= {"TAKE", "LEAN"}
    assert doc["og_chosen"] and doc["cards"] == {"nextgen": "Next Gen", "og": "OG"}


def test_the_poller_stays_awake_until_both_cards_are_chosen(pdb):
    def pend(t):
        return CD.pending(pdb, _Main(), t, t + timedelta(minutes=5), tip_fn, lambda m, s, d: TIP)

    assert pend(NOW)  # no lines yet: both cards wait
    with pdb.begin() as p:
        _v1(p, "gone", 0.6, live=False)  # lines now exist
    later = NOW + timedelta(minutes=30)
    CD.run(pdb, _Main(), later, tip_fn)
    assert pend(later)  # the shadow is still due
    [sh] = CD.run_shadow(pdb, _Main(), later, tip_fn)
    assert sh["picks"] == [] and not pend(later)


def test_shadow_ledger_rows_and_same_night_comparison(pdb, tmp_path):
    from betdata import ledger as L
    from betdata import official as O
    from betdata.ledger import ledger_dir

    with pdb.begin() as p:
        _pick(p, "e", 0.2)
        _v1(p, "v", 0.6)
        _v1(p, "w", 0.5, player="W")
    CD.run(pdb, _Main(), NOW, tip_fn)
    CD.run_shadow(pdb, _Main(), NOW, tip_fn)
    with pdb.connect() as p:
        L.sync(p, None, ledger_dir(), NOW)
    sh = {r["pick_id"]: r for r in L.read_rows(ledger_dir() / "shadow_card.csv")}
    assert {r["shadow"] for r in sh.values()} == {"yes"} and len(sh) == 2
    assert {r["signal_name"] for r in sh.values()} == {"card-v1-shadow"}
    days = L.read_rows(ledger_dir() / "card_days.csv")
    assert sorted((d["kind"], d["picks"]) for d in days) == [("engine", "1"), ("shadow", "2")]
    rows = O.dataset(ledger_dir())
    assert not any(r["official"] for r in rows if r["signal"] in ("value", "snipe"))
    c = O.comparison(rows, ledger_dir())
    assert c.nights == 1 and c.verdict.startswith("too early to compare")
    assert [m for m, _, _ in O.comparison_rows(c)] == [
        "Graded picks", "W-L-P", "Units", "ROI", "Average odds", "EV at close", "Calibration"]


def test_comparison_rule():
    from types import SimpleNamespace as NS

    from betdata import official as O

    def side(n, units, lo, hi):
        return O.Side("x", NS(graded=n, units=units), NS(ev=NS(low=lo, high=hi)))

    def verdict(e, s):
        return O.Comparison(10, e, s).verdict

    assert verdict(side(199, 1, -0.1, 0.0), side(300, 5, 0.1, 0.2)).startswith("too early")
    assert verdict(side(250, 1, -0.10, -0.02), side(250, 5, 0.01, 0.06)).startswith(
        "OG is ahead by the rule")
    assert verdict(side(250, 1, -0.10, 0.02), side(250, 5, 0.01, 0.06)) == (
        "no card is ahead by the rule")
    assert verdict(side(250, 1, -0.10, -0.02), side(250, -1, 0.01, 0.06)) == (
        "no card is ahead by the rule")  # units must be positive


def test_retiring_a_card_stops_its_selection_only(pdb, monkeypatch):
    """config/cards.yaml `active: false` retires a card: no new card; the other is unchanged."""
    from betdata import cards as C

    with pdb.begin() as p:
        _pick(p, "e", 0.2)
        _v1(p, "v", 0.6)
    real = C.active
    monkeypatch.setattr(C, "active", lambda k: k != "og" and real(k))
    assert CD.run_shadow(pdb, _Main(), NOW, tip_fn) == []
    [card] = CD.run(pdb, _Main(), NOW, tip_fn)
    assert [x["dedup_key"] for x in card["picks"]] == ["e"]
    monkeypatch.setattr(C, "active", lambda k: k != "nextgen" and real(k))
    with pdb.begin() as p:
        p.execute(alerts.delete())
    assert CD.run(pdb, _Main(), NOW + timedelta(days=1), tip_fn) == []


def test_research_table_and_rule_render(tmp_path):
    from betdata.page.engine_research import compare_html

    html = compare_html([], tmp_path)
    assert "<h2>Next Gen vs OG</h2>" in html and "were selected count (0 so far)" in html
    assert "<th>Next Gen</th><th>OG</th>" in html and "EV at close" in html
    assert "Comparison rule (fixed 2026-11-05)" in html and "owner&#x27;s decision" in html


def test_og_picks_up_to_five_by_its_own_rule_never_below_its_bar(pdb):
    """OG (owner 2026-11-05): top 5 by EV among Live F/C-NBA picks with the original M18
    labels; with fewer qualifying it shows fewer, never filled from below its bar."""
    with pdb.begin() as p:
        _v1(p, "a", 0.60, player="A")  # news edge 15 pp >= 1.5 x 6 pp: TAKE
        _v1(p, "b", 0.52, player="B")  # 7 pp: LEAN
        _v1(p, "gone", 0.70, player="G", live=False)  # not Live: never on the card
        _pick(p, "e", 0.9, player="E")  # an engine pick: Next Gen's pool, never OG's
    [og] = CD.run_shadow(pdb, _Main(), NOW, tip_fn)
    assert [(x["dedup_key"], x["label"], x["stake"]) for x in og["picks"]] == [
        ("a", "TAKE", 1.0), ("b", "LEAN", 0.5)]
    assert CD.card_note(og) == "2 picks today — OG found no other value"
    with pdb.connect() as p:
        title, body = CD.message(p, og, NOW)
    assert title == "Tonight's card - NBA OG - Thu Nov 5"
    assert body.splitlines()[-1] == "2 picks today — OG found no other value"
