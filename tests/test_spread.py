"""The Spread Picks card (owner decision 2026-11-10, betdata/spread.py; MODEL.md "Spread Picks
card"): trigger, pricing, selection after Next Gen, the ledger rows, voids and the backfill."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from betdata import spread as SP
from betdata.alerts import card as CD
from betdata.alerts.db import alert_grades, alerts, engine_for, get_state, quotes, set_state
from betdata.ledger import pick_id, read_rows

DAY = date(2026, 11, 11)
NOW = datetime(2026, 11, 11, 11, 15, tzinfo=UTC)  # Wed Nov 11, 6:15 AM ET
TIP = datetime(2026, 11, 12, 0, 0, tzinfo=UTC)  # 7:00 PM ET
MADE = NOW - timedelta(minutes=10)  # the ML pick's creation (the morning engine run)


@pytest.fixture
def pdb(tmp_path):
    return engine_for(tmp_path / "poller.sqlite")


def _alert(p, key, *, signal="engine", market="h2h", side="home", price=284,
           book="draftkings", gid=7, player=None, made=MADE, tip=TIP):
    p.execute(insert(alerts).values(
        signal=signal, sport="nba", game_id=gid, game_label="CLE @ CHA", tip=tip, market=market,
        player_name=player, side=side, line=None if market == "h2h" else 8.5, price=price,
        book=book, fair_prob=0.3, book_prob=0.25, reason="r", dedup_key=key, created_at=made,
        quiet=True, sent=False, details={}))  # fmt: skip


def _quote(p, book, outcome, line, price, at, gid=7, market="spreads", source="sharpapi"):
    p.execute(insert(quotes).values(polled_at=at, sport="nba", game_id=gid, source=source,
                                    book=book, market=market, outcome=outcome, line=line,
                                    price=price))  # fmt: skip


def _ng(p, picks, day=DAY, **extra):
    set_state(p, CD.key("nba", day), {"sport": "nba", "day": day.isoformat(),
                                      "source": "engine", "picks": picks, **extra}, NOW)


def _pick(key, label="LEAN"):
    return {"dedup_key": key, "label": label, "stake": 0.5 if label == "LEAN" else 1.0}


# --- trigger ------------------------------------------------------------------------------------


def test_trigger_only_next_gen_card_ml_underdogs(pdb):
    with pdb.begin() as p:
        _alert(p, "dog")  # the trigger
        _alert(p, "fav", price=-150)  # a favourite
        _alert(p, "even", price=100, side="away")  # +100: positive odds, a trigger
        _alert(p, "spread", market="spreads", price=-110)  # not h2h
        _alert(p, "offcard")  # an ML underdog NOT on the card
        _alert(p, "og", signal="snipe")  # OG's signal
        _alert(p, "news", signal="flash")  # a news alert
        for side, ln in (("home", 8.5), ("away", -8.5)):
            _quote(p, "draftkings", side, ln, -110, MADE - timedelta(minutes=5))
        _ng(p, [_pick("dog"), _pick("fav"), _pick("even", "TAKE"), _pick("spread"),
                _pick("og"), _pick("news"), {"dedup_key": "pass", "label": "PASS"}])
    got = SP.run(pdb, None, NOW)
    assert len(got) == 1
    trig = {x["trigger"] for x in got[0]["picks"]}
    assert trig == {pick_id("dog"), pick_id("even")}
    with pdb.connect() as p:
        sp = p.execute(select(alerts).where(alerts.c.signal == SP.SIGNAL)).all()
    assert {a.dedup_key for a in sp} == {SP.dedup(pick_id("dog")), SP.dedup(pick_id("even"))}
    assert all(a.market == "spreads" and a.sport == "nba" for a in sp)


def test_never_from_og_or_the_parlay_state(pdb):
    """Only Next Gen's stored card (card:nba:<day>) triggers; OG's (shadow:) and the parlay's
    states are never read."""
    with pdb.begin() as p:
        _alert(p, "dog")
        _quote(p, "draftkings", "home", 8.5, -110, MADE - timedelta(minutes=5))
        set_state(p, CD.shadow_key("nba", DAY), {"sport": "nba", "day": DAY.isoformat(),
                                                 "picks": [_pick("dog")]}, NOW)
        set_state(p, "parlay:nba:2026-11-11", {"legs": [{"dedup_key": "dog"}]}, NOW)
    assert SP.run(pdb, None, NOW) == []  # Next Gen not chosen: waits
    with pdb.connect() as p:
        assert get_state(p, SP.key(DAY)) is None


def test_waits_for_next_gen_then_chosen_once(pdb):
    with pdb.begin() as p:
        _alert(p, "dog")
        _quote(p, "draftkings", "home", 8.5, -110, MADE - timedelta(minutes=5))
    assert SP.run(pdb, None, NOW) == []
    with pdb.begin() as p:
        _ng(p, [_pick("dog")])
    assert len(SP.run(pdb, None, NOW)) == 1
    assert SP.run(pdb, None, NOW + timedelta(hours=1)) == []  # once per day


def test_empty_next_gen_gives_an_empty_spread_card(pdb):
    with pdb.begin() as p:
        _ng(p, [], no_lines=True)
    (c,) = SP.run(pdb, None, NOW)
    assert c["picks"] == [] and c["no_lines"] is True


def test_not_before_live_start(pdb):
    with pdb.begin() as p:
        _ng(p, [], day=date(2026, 11, 10))
    assert SP.run(pdb, None, datetime(2026, 11, 10, 11, 15, tzinfo=UTC)) == []


# --- pricing ------------------------------------------------------------------------------------


def _one(pdb):
    (c,) = SP.run(pdb, None, NOW)
    return c


def test_price_same_book_and_snapshot(pdb):
    with pdb.begin() as p:
        _alert(p, "dog", book="fanduel")
        _quote(p, "fanduel", "home", 9.0, -115, MADE - timedelta(hours=2))  # older line
        _quote(p, "fanduel", "home", 8.5, -108, MADE - timedelta(minutes=5))  # in force
        _quote(p, "fanduel", "home", 7.5, -110, MADE + timedelta(minutes=5))  # later
        _quote(p, "draftkings", "home", 8.0, -105, MADE - timedelta(minutes=1))
        _quote(p, "fanduel", "away", -8.5, -112, MADE - timedelta(minutes=5))  # other side
        _ng(p, [_pick("dog")])
    (x,) = _one(pdb)["picks"]
    assert (x["book"], x["line"], x["price"], x["price_source"]) == ("fanduel", 8.5, -108,
                                                                     "same book")
    with pdb.connect() as p:
        a = p.execute(select(alerts).where(alerts.c.signal == SP.SIGNAL)).one()
    assert (a.line, a.price, a.book, a.side) == (8.5, -108, "fanduel", "home")
    assert pick_id("dog") in a.reason and a.details["trigger"] == pick_id("dog")


def test_price_falls_back_to_the_other_book(pdb):
    with pdb.begin() as p:
        _alert(p, "dog", book="draftkings")
        _quote(p, "fanduel", "home", 8.0, -102, MADE - timedelta(minutes=5))
        _quote(p, "draftkings", "home", 7.5, -110, MADE + timedelta(minutes=5))  # after: no
        _ng(p, [_pick("dog")])
    (x,) = _one(pdb)["picks"]
    assert (x["book"], x["line"], x["price"], x["price_source"]) == ("fanduel", 8.0, -102,
                                                                     "other book")


def test_no_spread_skips_with_reason(pdb, tmp_path):
    with pdb.begin() as p:
        _alert(p, "dog")
        _quote(p, "pinnacle", "home", 8.5, -105, MADE - timedelta(minutes=5))  # not DK/FD
        _quote(p, "draftkings", "home", 8.5, -110, MADE + timedelta(minutes=5))  # too late
        _ng(p, [_pick("dog")])
    c = _one(pdb)
    assert c["picks"] == []
    assert c["skipped"] == [{"trigger": pick_id("dog"), "reason": SP.NO_SPREAD}]
    with pdb.connect() as p:
        assert not p.execute(select(alerts).where(alerts.c.signal == SP.SIGNAL)).all()
        SP.ledger_rows(p, tmp_path, NOW)
    (n,) = read_rows(tmp_path / "annotations.csv")
    assert (n["pick_id"], n["field"], n["value"]) == (pick_id("dog"), "spread_skipped",
                                                      SP.NO_SPREAD)


def test_one_spread_pick_per_ml_pick(pdb):
    with pdb.begin() as p:
        _alert(p, "dog")
        _quote(p, "draftkings", "home", 8.5, -110, MADE - timedelta(minutes=5))
        _ng(p, [_pick("dog")])
        c = SP.build(p, DAY, [_pick("dog")], NOW)
        SP.build(p, DAY, [_pick("dog")], NOW + timedelta(minutes=1))  # again: no duplicate
        n = len(p.execute(select(alerts).where(alerts.c.signal == SP.SIGNAL)).all())
    assert n == 1 and len(c["picks"]) == 1


# --- the ledger ---------------------------------------------------------------------------------


def test_ledger_rows_written_once(pdb, tmp_path):
    with pdb.begin() as p:
        _alert(p, "dog")
        _quote(p, "draftkings", "home", 8.5, -110, MADE - timedelta(minutes=5))
        _ng(p, [_pick("dog")])
    SP.run(pdb, None, NOW)
    for _ in range(2):
        with pdb.connect() as p:
            SP.ledger_rows(p, tmp_path, NOW)
    (r,) = read_rows(tmp_path / "spread_card.csv")
    assert r["pick_id"] == pick_id(SP.dedup(pick_id("dog")))
    assert (r["card_date"], r["trigger_pick_id"], r["spread_stake"], r["book"]) == (
        "2026-11-11", pick_id("dog"), "1.0", "draftkings")
    (d,) = read_rows(tmp_path / "card_days.csv")
    assert (d["kind"], d["card_date"], d["picks"]) == ("spread", "2026-11-11", "1")


def test_picks_csv_row_is_a_spreads_pick_naming_the_trigger(pdb, tmp_path):
    from betdata.ledger import sync

    with pdb.begin() as p:
        _alert(p, "dog")
        _quote(p, "draftkings", "home", 8.5, -110, MADE - timedelta(minutes=5))
        _ng(p, [_pick("dog")])
    SP.run(pdb, None, NOW)
    with pdb.connect() as p:
        sync(p, None, tmp_path, NOW)
    rows = {r["pick_id"]: r for r in read_rows(tmp_path / "picks.csv")}
    r = rows[pick_id(SP.dedup(pick_id("dog")))]
    assert (r["market"], r["signal"], r["signal_name"], r["line"], r["odds"]) == (
        "spreads", "dogsprd", "dog-spread", "8.5", "-110")
    assert pick_id("dog") in r["reason"]
    assert read_rows(tmp_path / "spread_card.csv")


# --- grading and voids --------------------------------------------------------------------------


def test_void_ml_voids_the_spread_pick(pdb):
    with pdb.begin() as p:
        _alert(p, "dog")
        _quote(p, "draftkings", "home", 8.5, -110, MADE - timedelta(minutes=5))
        _ng(p, [_pick("dog")])
    SP.run(pdb, None, NOW)
    with pdb.begin() as p:
        ids = {a.dedup_key: a.alert_id for a in p.execute(select(alerts))}
        p.execute(insert(alert_grades).values(alert_id=ids["dog"], result="void", profit=0.0,
                                              graded_at=NOW))
        p.execute(insert(alert_grades).values(alert_id=ids[SP.dedup(pick_id("dog"))],
                                              result="win", profit=0.9091, graded_at=NOW))
    assert SP.propagate_voids(pdb) == 1
    assert SP.propagate_voids(pdb) == 0
    with pdb.connect() as p:
        g = p.execute(select(alert_grades).where(
            alert_grades.c.alert_id == ids[SP.dedup(pick_id("dog"))])).one()
    assert (g.result, g.profit) == ("void", 0.0)


# --- backfill -----------------------------------------------------------------------------------


def _ledger_card(out, rows):
    import csv

    from betdata.ledger import CARD_FIELDS, PICK_FIELDS

    with (out / "card.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CARD_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with (out / "picks.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PICK_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows({"pick_id": r["pick_id"], "dedup_key": r["key"]} for r in rows)


def test_backfill_prices_from_stored_quotes_and_is_idempotent(pdb, tmp_path):
    d = date(2026, 11, 9)
    made = datetime(2026, 11, 9, 4, 30, tzinfo=UTC)
    tip = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
    with pdb.begin() as p:
        _alert(p, "cha", made=made, tip=tip)
        _alert(p, "den", side="away", price=260, book="fanduel", gid=8, made=made, tip=tip)
        _alert(p, "fav", price=-200, gid=9, made=made, tip=tip)
        _quote(p, "draftkings", "home", 8.5, -112, made - timedelta(minutes=30))
        _quote(p, "fanduel", "away", 6.5, -110, made + timedelta(hours=3), gid=8)  # later only
    rows = [{"pick_id": pick_id(k), "key": k, "sport": "nba", "card_date": d.isoformat(),
             "card": "yes", "card_label": "LEAN"} for k in ("cha", "den", "fav")]
    rows.append({"pick_id": pick_id("x"), "key": "x", "sport": "nba",
                 "card_date": "2026-11-05", "card": "yes", "card_label": "TAKE"})  # before
    _ledger_card(tmp_path, rows)
    (c,) = SP.backfill(pdb, tmp_path, NOW)
    assert c["backfill"] is True and c["day"] == "2026-11-09"
    got = {x["trigger"]: x for x in c["picks"]}
    assert set(got) == {pick_id("cha"), pick_id("den")}
    assert (got[pick_id("cha")]["line"], got[pick_id("cha")]["price_source"]) == (8.5,
                                                                                  "same book")
    assert (got[pick_id("den")]["line"], got[pick_id("den")]["price_source"]) == (
        6.5, "nearest later quote")
    assert SP.backfill(pdb, tmp_path, NOW) == []  # idempotent
    with pdb.connect() as p:
        SP.ledger_rows(p, tmp_path, NOW)
    notes = [n for n in read_rows(tmp_path / "annotations.csv")
             if n["field"] == "spread_backfill"]  # fmt: skip
    assert len(notes) == 2 and all(n["value"].startswith("reconstructed price: ") for n in notes)
    assert all(r["backfill"] == "true" for r in read_rows(tmp_path / "spread_card.csv"))
    assert SP.backfill(pdb, tmp_path, NOW) == []  # in the ledger now
