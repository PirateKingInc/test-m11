"""Tonight's card (alerts/card.py, MODEL.md "Tonight's card"): chosen once per sport and day,
30 minutes before the first game, from that day's picks still live, top 5 by expected value;
written once to the ledger (card yes/no, the card's label and stake); shown above Value."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from betdata import ledger as L
from betdata.alerts import card as CD
from betdata.alerts.db import alert_grades, alerts, engine_for, get_state, quotes, set_state

FIRST = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)  # Sat Oct 31, 8:00 PM ET
NOW = FIRST - timedelta(minutes=30)


@pytest.fixture
def pdb(tmp_path):
    return engine_for(tmp_path / "poller.sqlite")


def _pick(p, key, fair, price=110, signal="snipe", tip=FIRST, created=NOW - timedelta(hours=2),
          live=True, edge_source=None, line=6.5, player="A Player"):  # fmt: skip
    """A prop pick; with `live`, the book's latest quote is still its price."""
    d = {"edge": None if fair is None else fair - 0.45, "edge_source": edge_source}
    aid = p.execute(insert(alerts).values(
        signal=signal, sport="nba", game_id=7, game_label="NYK @ BOS", tip=tip,
        market="rebounds", player_name=player, side="over", line=line, price=price,
        book="draftkings", fair_prob=fair, book_prob=0.45, reason="r", dedup_key=key,
        created_at=created, quiet=False, sent=True, details=d)).inserted_primary_key[0]
    p.execute(insert(quotes).values(
        polled_at=NOW - timedelta(minutes=5), sport="nba", game_id=7, source="sharpapi",
        book="draftkings", market="rebounds", player_name=player, outcome="over", line=line,
        price=price if live else price - 60))  # fmt: skip
    return aid


class _Main:  # the card reads the first tip through tip_fn; the main database is not touched
    def connect(self):
        import contextlib

        return contextlib.nullcontext(None)


def tip_fn(_m, sport, day):
    return FIRST if sport == "nba" else None


def test_the_card_is_the_top_five_live_picks_by_expected_value(pdb):
    with pdb.begin() as p:
        for i, fair in enumerate((0.55, 0.60, 0.52, 0.58, 0.57, 0.53, 0.65)):
            _pick(p, f"k{i}", fair, player=f"P{i}")  # EV = fair x 2.1 - 1
        _pick(p, "gone", 0.70, live=False, player="Gone")
        _pick(p, "late", 0.70, created=NOW + timedelta(minutes=1), player="Late")
        _pick(p, "started", 0.70, tip=NOW - timedelta(minutes=1), player="Early")
        _pick(p, "g1", None, signal="g1", player="G")  # no fair price: no EV
    assert CD.run(pdb, _Main(), NOW - timedelta(minutes=1), tip_fn) == []  # not due yet
    [card] = CD.run(pdb, _Main(), NOW, tip_fn)
    assert [x["dedup_key"] for x in card["picks"]] == ["k6", "k1", "k3", "k4", "k0"]
    assert card["live"] == 7 and card["picks"][0]["ev"] == pytest.approx(0.65 * 2.1 - 1)
    # no Pinnacle and no news: LEAN, half a unit
    assert {(x["label"], x["stake"]) for x in card["picks"]} == {("LEAN", 0.5)}
    assert CD.run(pdb, _Main(), NOW + timedelta(minutes=5), tip_fn) == []  # chosen once
    with pdb.connect() as p:
        assert get_state(p, "card:nba:2026-10-31")["selected_at"] == NOW.isoformat()


def test_news_picks_at_one_and_a_half_thresholds_are_take(pdb):
    with pdb.begin() as p:
        _pick(p, "news", 0.60, signal="value", edge_source="news")  # edge 15 pp vs 6 pp prop
        _pick(p, "book", 0.60, signal="value", edge_source="book disagreement", player="B")
    [card] = CD.run(pdb, _Main(), NOW, tip_fn)
    assert {x["dedup_key"]: (x["label"], x["stake"]) for x in card["picks"]} == {
        "news": ("TAKE", 1.0), "book": ("LEAN", 0.5)}


def test_a_missed_window_records_no_card_and_cards_start_on_oct_31(pdb):
    with pdb.begin() as p:
        _pick(p, "k", 0.6)
    [card] = CD.run(pdb, _Main(), FIRST + timedelta(minutes=1), tip_fn)
    assert card["missed"] and card["picks"] == []
    assert CD.run(pdb, _Main(), datetime(2026, 10, 30, 22, 0, tzinfo=UTC), tip_fn) == []


def test_card_label_and_stake_are_written_once_in_the_ledger(pdb, tmp_path):
    out = tmp_path / "ledger"
    with pdb.begin() as p:
        a = _pick(p, "on", 0.62)
        _pick(p, "off", 0.70, live=False, player="Off")  # the price is gone: not chosen
    CD.run(pdb, _Main(), NOW, tip_fn)
    with pdb.begin() as p:
        _pick(p, "after", 0.70, created=NOW + timedelta(minutes=10), player="After")
        p.execute(insert(alert_grades).values(alert_id=a, result="win", profit=1.1,
                                              graded_at=FIRST))  # fmt: skip
    with pdb.connect() as p:
        L.sync(p, None, out, FIRST + timedelta(hours=4))
    first = (out / "card.csv").read_text()
    rows = {r["pick_id"]: r for r in L.read_rows(out / "card.csv")}
    on, off, after = (rows[L.pick_id(k)] for k in ("on", "off", "after"))
    assert (on["card"], on["card_label"], on["card_stake"]) == ("yes", "LEAN", "0.5")
    assert (off["card"], off["card_label"]) == ("no", "")
    assert after["card"] == "no"  # created after selection: stays off the card
    with pdb.begin() as p:  # the stored card changes later: the ledger keeps the first
        st = get_state(p, "card:nba:2026-10-31")
        st["picks"][0]["label"], st["picks"][0]["stake"] = "TAKE", 1.0
        set_state(p, "card:nba:2026-10-31", st, FIRST)
    with pdb.connect() as p:
        L.sync(p, None, out, FIRST + timedelta(hours=5))
    assert (out / "card.csv").read_text() == first
    with pytest.raises(L.AppendOnlyError):
        L.append_rows(out / "card.csv", (*L.CARD_FIELDS, "x"), [{"pick_id": "z"}])
    joined = {r["dedup_key"]: r for r in L.joined(out)}
    assert (joined["on"]["card"], joined["on"]["card_stake"]) == ("yes", "0.5")
    # creation labels are separate and untouched: these picks predate them
    assert joined["on"]["label"] == ""
    b = L.bank([r for r in joined.values() if r["card"] == "yes"], "card_label", "card_stake")
    assert (b.picks, b.wins, b.units, b.bankroll) == (1, 1, pytest.approx(0.55),
                                                      pytest.approx(100.55))


def test_a_rebuilt_poller_database_does_not_choose_again(pdb, tmp_path):
    from betdata.ledger import ledger_dir  # the tests' empty ledger folder

    with pdb.begin() as p:
        _pick(p, "on", 0.62)
    CD.run(pdb, _Main(), NOW, tip_fn)
    with pdb.connect() as p:
        L.sync(p, None, ledger_dir(), FIRST)
    fresh = engine_for(tmp_path / "rebuilt.sqlite")
    assert CD.run(fresh, _Main(), NOW + timedelta(minutes=2), tip_fn) == []


def test_card_section(pdb, tmp_path):
    from betdata.page.build import et
    from betdata.page.card import card_html

    out = tmp_path / "ledger"
    with pdb.begin() as p:
        a = _pick(p, "on", 0.62)
        _pick(p, "on2", 0.60, signal="value", edge_source="news", player="Two")
    CD.run(pdb, _Main(), NOW, tip_fn)
    with pdb.begin() as p:
        p.execute(insert(alert_grades).values(alert_id=a, result="win", profit=1.1,
                                              graded_at=FIRST))  # fmt: skip
    with pdb.connect() as p:
        L.sync(p, None, out, FIRST + timedelta(hours=4))
        rows = L.joined(out)
        html = card_html("nba", "NBA", rows, p, NOW + timedelta(minutes=5), et, None, rows)
    assert html.startswith("<section class='cardsec'><h2>Tonight's card</h2>")
    assert "<h3>Sat Oct 31</h3>" in html and "NYK @ BOS</b> · Sat 8:00 PM ET</div>" in html
    assert "<span class='lbl take'>TAKE 1u</span>" in html  # the news pick
    assert "<span class='lbl lean'>LEAN 0.5u</span> <span class='st live'>Win +0.55u</span>" in html
    assert "Card bankroll · All sports" in html and "Card bankroll · NBA" in html
    assert "<b>100.55u</b>" in html and "1-0-0 (2 card picks)" in html
    assert "C-NBA: " in html and "F: " in html  # the contributing signals' gate lines
    with pdb.connect() as p:
        assert p.execute(select(alerts.c.alert_id)).all()  # every pick stays a pick
