"""OG Ranking v2 (owner decision 2026-11-11, betdata/og_ranking.py; MODEL.md "OG Ranking v2")."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert

from betdata import eligibility as EL
from betdata import og_ranking as OR
from betdata.alerts import card as CD
from betdata.alerts.db import alerts, engine_for, quotes

NOW = datetime(2026, 11, 12, 11, 15, tzinfo=UTC)  # Thu Nov 12, 6:15 AM ET
TIP = datetime(2026, 11, 13, 0, 30, tzinfo=UTC)
_n = iter(range(10**6))


@pytest.fixture
def pdb(tmp_path, monkeypatch):
    monkeypatch.setattr(EL, "teammates_out", lambda *a: 0)
    monkeypatch.setattr(CD, "lines_exist", lambda *a: True)
    return engine_for(tmp_path / "poller.sqlite")


def _pick(p, key, *, fair=0.6, price=-110, line=4.5, side="over", median=5.0, player=None,
          market="assists", signal="snipe", made=None, book="draftkings", gid=7, mean=None,
          live=True):
    d = {"edge": 0.05, "edge_source": "book"}
    if median is not None:
        d["median"] = median
    if mean is not None:
        d["mean_after"] = mean
    p.execute(insert(alerts).values(
        signal=signal, sport="nba", game_id=gid, game_label="NYK @ BOS", tip=TIP, market=market,
        player_name=player or key, side=side, line=line, price=price, book=book,
        fair_prob=fair, book_prob=0.5, reason="r", dedup_key=key,
        created_at=made or NOW - timedelta(hours=2), quiet=True, sent=False, details=d))
    if live:
        p.execute(insert(quotes).values(
            polled_at=NOW - timedelta(minutes=5 + next(_n) % 3), sport="nba", game_id=gid,
            source="sharpapi", book=book, market=market, player_name=player or key,
            outcome=side, line=line, price=price))  # fmt: skip


class _Main:
    def connect(self):
        import contextlib

        return contextlib.nullcontext(None)


def _og(pdb, now=NOW):
    out = CD.run_shadow(pdb, _Main(), now, lambda m, s, d: TIP if s == "nba" else None)
    return out[0]


def _keys(c):
    return [x["dedup_key"] for x in c["picks"]]


@pytest.mark.parametrize("price,ok", [(-150, True), (130, True), (-151, False), (131, False),
                                      (100, True), (-110, True), (250, False)])
def test_price_range_bounds(pdb, price, ok):
    with pdb.begin() as p:
        _pick(p, "a", price=price)
    c = _og(pdb)
    assert (_keys(c) == ["a"]) is ok
    if not ok:
        assert c["og_rejected"] == {"a": OR.PRICE}


@pytest.mark.parametrize("side,median,ok", [("over", 5.0, True), ("over", 4.5, False),
                                            ("over", 4.0, False), ("under", 4.0, True),
                                            ("under", 4.5, False), ("under", 5.0, False)])
def test_median_side_equal_is_rejected(pdb, side, median, ok):
    with pdb.begin() as p:
        _pick(p, "a", side=side, median=median, line=4.5)
    c = _og(pdb)
    assert (_keys(c) == ["a"]) is ok
    if not ok:
        assert c["og_rejected"]["a"] == OR.MEDIAN_SIDE


def test_missing_median_rejected_and_c_nba_mean_after_used(pdb):
    with pdb.begin() as p:
        _pick(p, "none", median=None)
        _pick(p, "cnba", median=None, mean=5.2, signal="value")  # C-NBA: the projection
    c = _og(pdb)
    assert _keys(c) == ["cnba"] and c["og_rejected"] == {"none": OR.NO_MEDIAN}


def test_one_pick_per_player_highest_probability(pdb):
    with pdb.begin() as p:
        _pick(p, "dk", player="Jalen", fair=0.58, line=5.5, median=6.0)
        _pick(p, "fd", player="Jalen", fair=0.63, line=4.5, book="fanduel")
        _pick(p, "other", player="Mikal", fair=0.55)
        _pick(p, "othergame", player="Jalen", fair=0.5, gid=8)  # another game: its own pick
    c = _og(pdb)
    assert _keys(c) == ["fd", "other", "othergame"]
    assert c["og_rejected"] == {"dk": OR.DUPLICATE}


def test_ranking_order_and_tie_breaks(pdb):
    with pdb.begin() as p:
        _pick(p, "low", fair=0.52)
        _pick(p, "high", fair=0.66, price=120)
        _pick(p, "tie_long", fair=0.6, price=110)
        _pick(p, "tie_short", fair=0.6, price=-120)
        _pick(p, "tie_short_late", fair=0.6, price=-120, made=NOW - timedelta(hours=1))
    c = _og(pdb)
    assert _keys(c) == ["high", "tie_short", "tie_short_late", "tie_long", "low"]
    x = c["picks"][0]
    assert (x["fair_prob"], x["price"], x["median"], x["stake"]) == (0.66, 120, 5.0, 0.5)
    assert all(y["stake"] == 0.5 and y["label"] in ("TAKE", "LEAN") for y in c["picks"])


def test_up_to_five_rest_outside_top_5(pdb):
    with pdb.begin() as p:
        for i in range(7):
            _pick(p, f"k{i}", fair=0.7 - i / 100)
    c = _og(pdb)
    assert _keys(c) == [f"k{i}" for i in range(5)]
    assert c["og_rejected"] == {"k5": OR.OUTSIDE, "k6": OR.OUTSIDE}


def test_fewer_qualify_fewer_published_never_filled(pdb):
    with pdb.begin() as p:
        _pick(p, "ok1")
        _pick(p, "ok2", fair=0.55)
        _pick(p, "long", price=200, fair=0.9)  # EV-best, ineligible
        _pick(p, "wrong", median=4.0, fair=0.9)
    c = _og(pdb)
    assert _keys(c) == ["ok1", "ok2"]
    assert CD.card_note(c) == "2 picks today — OG found no other value"


def test_none_qualify_empty_card(pdb):
    with pdb.begin() as p:
        _pick(p, "long", price=200)
    c = _og(pdb)
    assert c["picks"] == [] and CD.card_note(c) == "No picks today — OG found no value"


def test_rules_v2_applied_first(pdb):
    with pdb.begin() as p:
        _pick(p, "reb", market="rebounds", fair=0.9)
        _pick(p, "u55", side="under", line=5.5, median=4.0, fair=0.9)
        _pick(p, "ok")
    c = _og(pdb)
    assert _keys(c) == ["ok"]
    assert c["filtered"] == {"reb": EL.REBOUNDS, "u55": EL.UNDER_ASSISTS}
    assert "reb" not in c.get("og_rejected", {}) and "u55" not in c.get("og_rejected", {})


def test_not_live_never_chosen(pdb):
    with pdb.begin() as p:
        _pick(p, "gone", live=False, fair=0.9)
        _pick(p, "ok")
    assert _keys(_og(pdb)) == ["ok"]


def test_before_nov_12_the_ev_rule_is_unchanged(pdb):
    """Nov 11's card would be EV-ranked exactly as before (choose with V1_SIGNALS)."""
    nov11 = NOW - timedelta(days=1)
    with pdb.begin() as p:
        _pick(p, "long", price=200, fair=0.45, made=nov11 - timedelta(hours=2))
    with pdb.connect() as p:
        a = CD.choose_og(p, "nba", nov11.date(), nov11, None)
        b = CD.choose(p, "nba", nov11.date(), nov11, CD.V1_SIGNALS, None)
    assert a == b and "ranking" not in a


def test_rejections_written_once_as_annotations(pdb, tmp_path):
    from betdata.ledger import pick_id, read_rows

    with pdb.begin() as p:
        _pick(p, "long", price=200)
        _pick(p, "ok")
    _og(pdb)
    for _ in range(2):
        with pdb.connect() as p:
            OR.annotation_rows(p, tmp_path, NOW)
    (n,) = read_rows(tmp_path / "annotations.csv")
    assert (n["pick_id"], n["field"], n["value"]) == (pick_id("long"), "og_rank", OR.PRICE)


def test_filters_table_rows_for_og():
    rows = [{"sport": "nba", "og_rank": OR.PRICE, "result": "win", "profit": "1.5",
             "odds": "150", "signal_name": "F", "clv_consensus": "", "official": False},
            {"sport": "nba", "og_rank": OR.PRICE, "result": "loss", "profit": "-1",
             "odds": "160", "signal_name": "F", "clv_consensus": "", "official": False}]
    (r,) = OR.filters_table(rows)
    assert (r["card"], r["reason"], r["removed"], r["wlp"], r["units"]) == (
        "OG", OR.PRICE, 2, "1-1-0", 0.5)


# --- the OG Top 3 shadow ----------------------------------------------------------------------


def test_top3_chosen_in_the_same_run_and_ledgered(pdb, tmp_path):
    from betdata.alerts.db import get_state
    from betdata.ledger import pick_id, read_rows

    with pdb.begin() as p:
        for i in range(6):
            _pick(p, f"k{i}", fair=0.7 - i / 100)
    og = _og(pdb)
    with pdb.connect() as p:
        t3 = get_state(p, OR.top3_key("nba", NOW.date()))
        OR.top3_rows(p, tmp_path, NOW)
        OR.top3_rows(p, tmp_path, NOW)  # write-once
    assert [x["dedup_key"] for x in t3["picks"]] == _keys(og)[:3] == ["k0", "k1", "k2"]
    assert t3["selected_at"] == og["selected_at"] and t3["source"] == OR.TOP3_SOURCE
    rows = read_rows(tmp_path / "og_top3.csv")
    assert [r["pick_id"] for r in rows] == [pick_id(k) for k in ("k0", "k1", "k2")]
    assert [r["rank"] for r in rows] == ["1", "2", "3"] and rows[0]["og3_stake"] == "0.5"
    (d,) = read_rows(tmp_path / "card_days.csv")
    assert (d["kind"], d["picks"]) == ("og3", "3")


def test_top3_never_pushed_shown_or_official(pdb, tmp_path):
    """The push and the page never read the Top 3 state or file; official records ignore it."""
    import inspect

    from betdata import cards as C
    from betdata import morning as M
    from betdata import official as O
    from betdata.page import official as PO
    from betdata.page import tracker as PT

    for mod in (M, PO, PT, C, O):
        src = inspect.getsource(mod)
        if mod is M:  # the morning run reads it only for its run report
            body = src.split("def nba_message")[1].split("\ndef ")[0]
            assert "og3" not in body and "top3" not in body.lower()
        else:
            assert "og3" not in src and "og_top3" not in src and "top3" not in src.lower()
    from tests.test_spread_records import _ledger

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _ledger(a)
    _ledger(b)
    (b / "og_top3.csv").write_text(",".join(OR.TOP3_FIELDS) + "\nog1,nba,2026-11-09,yes,LEAN,"
                                   "0.5,,1,0.6,120,7.0,6.5\n")
    va, vb = C.view(O.dataset(a), C.OG), C.view(O.dataset(b), C.OG)
    assert va == vb


# --- the record marker ------------------------------------------------------------------------


def _off(pid, day, res, profit):
    return {"pick_id": pid, "sport": "nba", "official": True, "day": day, "result": res,
            "profit": profit, "card_stake": "0.5", "odds": "110", "signal_name": "F",
            "clv_consensus": "", "game": "NYK @ BOS", "tip": f"{day}T23:30:00+00:00",
            "player": pid, "market": "assists", "side": "over", "line": "4.5",
            "book": "draftkings", "game_id": 7, "created_at": f"{day}T11:00:00+00:00"}


def test_og_record_marker_and_since_ranking_v2(tmp_path):
    import re
    from datetime import date

    from betdata import official as O
    from betdata.page.official import card_header

    off = [_off("a", "2026-11-10", "loss", "-1"), _off("b", "2026-11-11", "win", "1.1"),
           _off("c", "2026-11-12", "win", "0.9"), _off("d", "2026-11-12", "loss", "-1"),
           _off("e", "2026-11-13", "", "")]  # pending: never counted
    h = card_header(off, "nba", "NBA", date(2026, 11, 13), tmp_path, "og", "OG")
    assert "<b>Ranking v2 from Nov 12</b>" in h
    rows = dict(re.findall(r"<dt>([^<]*)</dt><dd><span class='rec'[^>]*><a [^>]*><b>([^<]*)",
                           h))
    assert rows["All time"] == "2-2-0"  # the full history, unchanged
    assert rows["Since ranking v2 (Nov 12)"] == "1-1-0"  # Ranking v2 picks only
    assert list(rows)[:3] == ["All time", "Since rules v2 (Nov 10)", "Since ranking v2 (Nov 12)"]
    assert O.record(off, O.official_stake).graded == 4
    ng = card_header(off, "nba", "NBA", date(2026, 11, 13), tmp_path, "nextgen", "Next Gen")
    assert "Ranking v2" not in ng


# --- the slump alert --------------------------------------------------------------------------


def _og_rows(results_by_day):
    """OG card rows (official.dataset shape, as C.view(rows, OG) reads them)."""
    rows, i = [], 0
    for day, res in results_by_day:
        for r in res:
            i += 1
            rows.append({"pick_id": f"p{i}", "sport": "nba", "official": False,
                         "signal_name": "F", "shadow": True, "shadow_date": day,
                         "shadow_stake": "0.5", "shadow_label": "LEAN", "day": day,
                         "result": r, "profit": {"win": "1.0", "loss": "-1.0"}.get(r, ""),
                         "odds": "100", "tip": f"{day}T23:{i % 60:02d}:00+00:00",
                         "created_at": f"{day}T11:00:00+00:00", "clv_consensus": ""})
    return rows


def _days(*res):
    return [(f"2026-11-{12 + i}", r) for i, r in enumerate(res)]


def test_slump_fires_at_3_12_not_4_11():
    l5, w1l4 = ["loss"] * 5, ["win"] + ["loss"] * 4
    assert OR.slump(_og_rows(_days(w1l4, w1l4, w1l4))) == (3, 12)
    w2l3 = ["win"] * 2 + ["loss"] * 3
    assert OR.slump(_og_rows(_days(w2l3, w1l4, w1l4))) is None  # 4-11
    assert OR.slump(_og_rows(_days(l5, l5, w1l4))) == (1, 14)  # worse fires too
    assert OR.slump(_og_rows(_days(l5, l5, ["loss"] * 4))) is None  # 14 graded: never
    assert OR.slump(_og_rows(_days(l5, l5, l5 + [""]))) == (0, 15)  # pending never counts
    # only the most recent 3 card days: an older bad day is outside the window
    assert OR.slump(_og_rows(_days(l5, ["win"] * 5, w1l4, w1l4))) is None
    # Ranking v2 picks only: days before Nov 12 never count
    old = _og_rows([("2026-11-10", l5), ("2026-11-11", l5)])
    assert OR.slump(old + _og_rows(_days(l5))) is None


def test_slump_alert_once_a_day(tmp_path):
    from betdata.alerts.db import engine_for, get_state

    pdb = engine_for(tmp_path / "p.sqlite")
    sent = []

    class Client:
        def post(self, url, content, headers):
            sent.append((headers["Title"], content.decode()))
            return type("R", (), {"status_code": 200})()

    w1l4 = ["win"] + ["loss"] * 4
    rows = _og_rows(_days(w1l4, w1l4, w1l4))
    now = datetime(2026, 11, 15, 11, 16, tzinfo=UTC)
    assert OR.slump_check(pdb, rows, now, "t", Client()) == "http 200"
    assert OR.slump_check(pdb, rows, now, "t", Client()) is None  # once a day
    assert sent == [("OG review", "OG review: 3-12 over the last 3 days — check "
                                  "reports/nba/og_ranking_v2.md")]
    with pdb.connect() as p:
        assert get_state(p, "og-slump:2026-11-15")["status"] == "http 200"
    ok = _og_rows(_days(["win"] * 5, ["win"] * 5, ["win"] * 5))
    assert OR.slump_check(pdb, ok, now + timedelta(days=1), "t", Client()) is None
    assert len(sent) == 1
