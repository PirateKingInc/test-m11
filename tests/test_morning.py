"""The 6:15 AM ET morning update (owner decision 2026-11-05, betdata/morning.py): grade, ledger,
engine, select both cards, publish, then ONE push per sport combining last night, the record and
today's card, with a line for any game still ungraded."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert

from betdata import morning as M
from betdata.alerts import card as CD
from betdata.alerts.db import alerts, engine_for, get_state, quotes, set_state
from betdata.runner import window as W

NOW = datetime(2026, 11, 6, 11, 15, tzinfo=UTC)  # Fri Nov 6, 6:15 AM ET (EST)
TIP = datetime(2026, 11, 7, 0, 30, tzinfo=UTC)  # 7:30 PM ET tonight


@pytest.fixture
def pdb(tmp_path):
    return engine_for(tmp_path / "poller.sqlite")


def test_order_grade_before_select(pdb, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(M, "sports_due", lambda main, now: ["nba"])
    monkeypatch.setattr(M, "grade", lambda poller, now: calls.append("grade"))
    monkeypatch.setattr(M, "ledger_sync", lambda *a: calls.append("ledger"))
    monkeypatch.setattr(M, "engine", lambda *a: calls.append("engine") or 2)
    monkeypatch.setattr(M, "select_cards", lambda *a: calls.append("select") or (
        [{"sport": "nba", "picks": [1, 2]}], [{"sport": "nba", "picks": [1]}]))
    rep = M.run(None, pdb, None, NOW, out=tmp_path, build=lambda: calls.append("page"))
    assert rep.steps == ["grade", "ledger", "engine", "select", "publish"]
    # the ledger is synced again after selection (card rows), then the page is built
    assert calls == ["grade", "ledger", "engine", "select", "ledger", "page"]
    assert rep.cards == {"nba": 2} and rep.shadow == {"nba": 1} and rep.engine_picks == 2
    with pdb.connect() as p:
        st = get_state(p, "morning:2026-11-06")
    assert st["steps"] == rep.steps and st["sports"] == ["nba"]


def test_a_failed_step_does_not_stop_the_rest(pdb, monkeypatch, tmp_path):
    monkeypatch.setattr(M, "sports_due", lambda main, now: ["nba"])

    def boom(*a):
        raise RuntimeError("box score source down")

    monkeypatch.setattr(M, "grade", boom)
    monkeypatch.setattr(M, "ledger_sync", lambda *a: None)
    monkeypatch.setattr(M, "engine", lambda *a: 0)
    monkeypatch.setattr(M, "select_cards", lambda *a: ([], []))
    rep = M.run(None, pdb, None, NOW, out=tmp_path, build=lambda: None)
    assert rep.steps == list(M.STEPS) and rep.errors[0].startswith("grade: RuntimeError")


def test_no_games_yesterday_or_today_does_nothing(pdb, monkeypatch, tmp_path):
    monkeypatch.setattr(M, "sports_due", lambda main, now: [])
    monkeypatch.setattr(M, "grade", lambda *a: pytest.fail("nothing to grade"))
    assert M.run(None, pdb, None, NOW, out=tmp_path).steps == []


def _row(pid, day, res, profit, stake, signal="nba-engine", game="ORL @ MIA", tip=None,
         odds="110", og=False):
    """A ledger row: a Next Gen card pick (official), or with `og` an OG card pick (shadow)."""
    r = {"pick_id": pid, "sport": "nba", "official": not og, "day": day, "result": res,
         "profit": profit, "card_stake": "" if og else stake, "odds": odds,
         "signal_name": signal, "game": game, "tip": tip or f"{day}T23:30:00+00:00",
         "clv_consensus": "", "game_id": 1, "market": "points", "player": pid, "side": "over",
         "shadow": og, "shadow_date": day if og else "", "shadow_stake": stake if og else "",
         "shadow_label": "LEAN" if og else ""}
    return r


def _alert(p, signal, key, player, market="points", price=-110):
    p.execute(insert(alerts).values(
        signal=signal, sport="nba", game_id=9, game_label="BOS @ NYK", tip=TIP, market=market,
        player_name=player, side="over", line=20.5, price=price, book="draftkings",
        fair_prob=0.6, book_prob=0.5, reason="r", dedup_key=key,
        created_at=NOW - timedelta(minutes=5), quiet=True, sent=False,
        details={"ev": 0.15, "theta": 0.1}))  # fmt: skip


def _card_today(pdb, n=1, og=None):
    """Next Gen with `n` engine picks; OG with the given F picks (players), or not chosen."""
    with pdb.begin() as p:
        picks = []
        for i in range(n):
            key = f"engine:9:points:p{i}"
            _alert(p, "engine", key, f"Player {i}")
            picks.append({"dedup_key": key, "label": "TAKE", "stake": 1.0})
        set_state(p, CD.key("nba", NOW.date()), {"sport": "nba", "day": "2026-11-06",
                                                 "source": "engine", "picks": picks}, NOW)
        if og is not None:
            ogp = []
            for name in og:
                key = f"snipe:9:points:{name}"
                _alert(p, "snipe", key, name, price=120)
                ogp.append({"dedup_key": key, "label": "LEAN", "stake": 0.5})
            set_state(p, CD.shadow_key("nba", NOW.date()),
                      {"sport": "nba", "day": "2026-11-06", "source": CD.SHADOW,
                       "picks": ogp}, NOW)  # fmt: skip


def test_combined_push_content(pdb):
    """One NBA push: OG then Next Gen, each with last night, its record and today's card."""
    rows = [_row("a", "2026-11-05", "win", "1.0", "1.0"),
            _row("b", "2026-11-05", "win", "1.0", "0.5"),
            _row("c", "2026-11-05", "loss", "-1.0", "0.5"),
            _row("d", "2026-11-04", "win", "1.0", "1.0", signal="F"),  # Card v1 = OG's history
            _row("o1", "2026-11-05", "win", "1.0", "0.5", signal="F", og=True),  # excluded day
            _row("o2", "2026-11-06", "", "", "0.5", signal="F", og=True)]  # fmt: skip
    _card_today(pdb, 2, og=["Player 1", "Other Guy"])
    with pdb.connect() as p:
        title, body = M.message(p, rows, "nba", NOW)
    assert title == "Morning update - NBA - Fri Nov 6"
    assert body.split("\n") == [
        "NBA · Fri Nov 6",
        "OG: last night 0-0, +0.0u · record 1-0, +1.0u (1 of 500)",
        "Today: 2 picks —",
        "  BOS @ NYK 7:30 PM ET: Player 1 over 20.5 points +120 at DraftKings, LEAN 0.5u "
        "(on both cards)",
        "  BOS @ NYK 7:30 PM ET: Other Guy over 20.5 points +120 at DraftKings, LEAN 0.5u",
        "  2 picks today — OG found no other value",
        "Next Gen: last night 2-1, +1.0u · record 2-1, +1.0u (3 of 500)",
        "Today: 2 picks —",
        "  BOS @ NYK 7:30 PM ET: Player 0 over 20.5 points -110 at DraftKings, TAKE 1u",
        "  BOS @ NYK 7:30 PM ET: Player 1 over 20.5 points -110 at DraftKings, TAKE 1u "
        "(on both cards)",
        "  2 picks today — the engine found no other value"]


def test_empty_next_gen_and_og_waiting_for_lines(pdb):
    _card_today(pdb, 0)  # Next Gen chosen empty; OG not chosen (no lines when it was due)
    with pdb.connect() as p:
        _t, body = M.message(p, [], "nba", NOW)
    lines = body.split("\n")
    assert lines[2] == ("Today: no lines posted yet for today's games — chosen at the first "
                        "later poll with lines")
    assert lines[4] == "Today: No picks today — the engine found no value"


def test_an_ungraded_late_game_is_named_in_the_push(pdb):
    late_tip = "2026-11-06T03:30:00+00:00"  # Thu 10:30 PM ET, West Coast: no box score yet
    rows = [_row("a", "2026-11-05", "win", "1.0", "1.0"),
            _row("z", "2026-11-05", "", "", "0.5", game="LAL @ POR", tip=late_tip)]
    _card_today(pdb, 5, og=[])
    with pdb.connect() as p:
        _t, body = M.message(p, rows, "nba", NOW)
    assert "Next Gen: last night 1-0, +1.0u" in body  # the ungraded pick is not in the record
    assert body.splitlines()[-1] == ("Not graded yet: LAL @ POR (no final box score yet; graded "
                                     "on the next run)")
    assert M.ungraded_games(rows, "nba", NOW.date() - timedelta(days=1)) == ["LAL @ POR"]


def test_nfl_keeps_its_own_push(pdb):
    with pdb.connect() as p:
        _t, body = M.message(p, [], "nfl", NOW)
    assert body.startswith("NFL · Last night: no graded card picks · Card 0-0, +0.0u")


def test_one_push_per_sport_and_the_card_is_not_pushed_again(pdb, tmp_path):
    import httpx

    _card_today(pdb, 2, og=["Other Guy"])
    with pdb.begin() as p:
        set_state(p, "morning:2026-11-06", {"sports": ["nba"]}, NOW)
    posts = []
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: posts.append(r) or httpx.Response(200)))
    assert M.push(pdb, tmp_path, NOW, "topic-x", client) == {"nba": "http 200"}
    assert len(posts) == 1 and posts[0].headers["Title"] == "Morning update - NBA - Fri Nov 6"
    with pdb.connect() as p:  # both NBA cards were in it: neither is pushed again
        assert get_state(p, CD.key("nba", NOW.date()))["push"] == "morning http 200"
        assert get_state(p, CD.shadow_key("nba", NOW.date()))["push"] == "morning http 200"


def test_poller_stops_at_612_on_morning_days(monkeypatch):
    end = datetime(2026, 11, 6, 11, 56, tzinfo=UTC)  # 6:56 AM ET
    six = datetime(2026, 11, 6, 11, 0, 30, tzinfo=UTC)
    monkeypatch.setattr(M, "sports_due", lambda main, now: ["nba"])
    assert M.poller_until(None, six, end) == datetime(2026, 11, 6, 11, 12, tzinfo=UTC)
    assert M.poller_until(None, six + timedelta(hours=1), end) == end  # 7 AM: unchanged
    monkeypatch.setattr(M, "sports_due", lambda main, now: [])
    assert M.poller_until(None, six, end) == end  # no games: the usual :56


def test_the_6am_sweep_finishes_by_610():
    from betdata.alerts.archive import games_per_cycle

    assert games_per_cycle("2026-11-06:0600") == 6 and games_per_cycle("2026-11-06:0700") == 3


def test_clock_starts_the_morning_update_at_615():
    six = datetime(2026, 11, 6, 11, 0, 31, tzinfo=UTC)  # the clock run that starts at 6 AM ET
    slept = []
    assert W.morning_due(six) and not W.morning_due(six + timedelta(hours=1))
    assert not W.morning_due(datetime(2026, 11, 6, 11, 16, tzinfo=UTC))  # after 6:15: too late
    W.morning_main(six, slept.append)
    assert slept == [pytest.approx(14 * 60 + 29)]
    assert W.morning_at(six) == datetime(2026, 11, 6, 11, 15, tzinfo=UTC)


def test_the_card_is_selected_at_615():
    assert CD.select_at(NOW.date()) == NOW


def test_stored_quotes_are_the_latest_per_side(pdb):
    with pdb.begin() as p:
        for at, price in ((NOW - timedelta(hours=2), -110), (NOW - timedelta(minutes=8), -125)):
            p.execute(insert(quotes).values(
                polled_at=at, sport="nba", game_id=9, source="sharpapi", book="draftkings",
                market="spreads", outcome="home", line=-3.5, price=price))
    with pdb.connect() as p:
        [q] = M.stored_quotes(p, 9, NOW)
    assert q.price == -125 and q.book == "draftkings"
