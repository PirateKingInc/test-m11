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
          live=True, details=None):
    d = dict(details or {"edge": 0.05, "edge_source": "book"})
    pn = None if market == "h2h" else (player or key)
    if median is not None:
        d["median"] = median
    if mean is not None:
        d["mean_after"] = mean
    p.execute(insert(alerts).values(
        signal=signal, sport="nba", game_id=gid, game_label="NYK @ BOS", tip=TIP, market=market,
        player_name=pn, side=side, line=line, price=price, book=book,
        fair_prob=fair, book_prob=0.5, reason="r", dedup_key=key,
        created_at=made or NOW - timedelta(hours=2), quiet=True, sent=False, details=d))
    if live:
        p.execute(insert(quotes).values(
            polled_at=NOW - timedelta(minutes=5 + next(_n) % 3), sport="nba", game_id=gid,
            source="sharpapi", book=book, market=market, player_name=pn,
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


def test_slump_fires_at_3_12_over_15_in_3_days_not_4_11():
    L5 = ["loss"] * 5
    # 3-12 over 15 / 3 days, the last 9 at 3-6 (so only the 15-pick rule fires)
    rows = _og_rows(_days(L5, ["loss"] * 4 + ["win"], ["win", "win", "loss", "loss", "loss"]))
    assert OR.slump_rule(rows) == ("3 days", 3, 12)
    # 4-11 over 15, the last 9 at 4-5: nothing fires
    rows = _og_rows(_days(L5, ["loss"] * 4 + ["win"], ["win", "win", "win", "loss", "loss"]))
    assert OR.slump_rule(rows) is None
    # only the most recent 3 card days and Ranking v2 picks count
    old = _og_rows([("2026-11-10", L5), ("2026-11-11", L5)])
    assert OR.slump_rule(old) is None


def test_slump_fires_at_2_7_over_the_last_9_not_3_6():
    two = _og_rows(_days(["win", "loss", "loss"], ["loss", "win", "loss"], ["loss"] * 3))
    assert OR.slump_rule(two) == ("9 picks", 2, 7)  # 9 picks over 3 short days
    three = _og_rows(_days(["win", "loss", "loss"], ["loss", "win", "loss"],
                           ["loss", "win", "loss"]))
    assert OR.slump_rule(three) is None  # 3-6
    assert OR.slump_rule(_og_rows(_days(["loss"] * 8))) is None  # 8 decided: never
    assert OR.slump_rule(_og_rows(_days(["loss"] * 9))) == ("9 picks", 0, 9)  # one day too


def test_slump_excludes_pushes_voids_and_pending():
    # the last 9 decided picks are 2-7; the push, void and pending pick are skipped, not counted
    rows = _og_rows(_days(["win", "loss", "loss", "push"], ["loss", "win", "void", "loss"],
                          ["loss", "loss", "", "loss"]))
    assert OR.slump_rule(rows) == ("9 picks", 2, 7)
    # with the push counted as a decided pick the last 9 would be 2-6-1: never like that
    assert OR.slump(rows) == (2, 7)


def test_slump_alert_once_a_day(tmp_path):
    from betdata.alerts.db import engine_for, get_state

    pdb = engine_for(tmp_path / "p.sqlite")
    sent = []

    class Client:
        def post(self, url, content, headers):
            sent.append((headers["Title"], content.decode()))
            return type("R", (), {"status_code": 200})()

    rows = _og_rows(_days(["loss"] * 5, ["loss"] * 4 + ["win"],
                          ["win", "win", "loss", "loss", "loss"]))  # 3-12: both rules checked
    now = datetime(2026, 11, 15, 11, 16, tzinfo=UTC)
    assert OR.slump_check(pdb, rows, now, "t", Client()) == "http 200"
    assert OR.slump_check(pdb, rows, now, "t", Client()) is None  # once a day
    short = _og_rows(_days(["loss"] * 9))  # the 9-pick rule the same day: still once a day
    assert OR.slump_check(pdb, short, now, "t", Client()) is None
    assert sent == [("OG review", "OG review: 3-12 over the last 3 days — check "
                                  "reports/nba/og_ranking_v2.md")]
    assert OR.slump_check(pdb, short, now + timedelta(days=1), "t", Client()) == "http 200"
    assert sent[-1][1] == ("OG review: 0-9 over the last 9 graded picks — check "
                           "reports/nba/og_ranking_v2.md")
    with pdb.connect() as p:
        assert get_state(p, "og-slump:2026-11-15")["status"] == "http 200"
    ok = _og_rows(_days(["win"] * 5, ["win"] * 5, ["win"] * 5))
    assert OR.slump_check(pdb, ok, now + timedelta(days=2), "t", Client()) is None
    assert len(sent) == 2


# --- the review: report, milestone, backtest ----------------------------------------------------


def _ledger_og(out, n_days, per_day=5, results=None):
    """A ledger with OG Ranking v2 card picks from Nov 12 (shadow_card.csv) and a Top 3."""
    import csv

    from betdata.ledger import CARD_DAY_FIELDS, PICK_FIELDS, RESULT_FIELDS, SHADOW_FIELDS

    def w(name, fields, rows):
        with (out / name).open("w", newline="") as f:
            x = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
            x.writeheader()
            x.writerows(rows)

    picks, res, sh, t3, days = [], [], [], [], []
    for d in range(n_days):
        day = f"2026-11-{12 + d}"
        days += [{"key": f"nba:{day}:{k}", "sport": "nba", "card_date": day, "kind": k,
                  "picks": "x"} for k in ("shadow", "og3")]
        for i in range(per_day):
            pid = f"P{d}{i}"
            r = (results or (lambda d, i: "win" if i % 2 == 0 else "loss"))(d, i)
            picks.append({"pick_id": pid, "dedup_key": pid, "sport": "nba", "game_id": "7",
                          "game": "NYK @ BOS", "tip": f"{day}T23:30:00+00:00",
                          "created_at": f"{day}T11:00:00+00:00", "market": "assists",
                          "player": pid, "side": "over", "line": "4.5", "odds": "-110",
                          "book": "draftkings", "signal": "snipe", "signal_name": "F"})
            res.append({"pick_id": pid, "result": r,
                        "profit": "0.9091" if r == "win" else "-1.0"})
            sh.append({"pick_id": pid, "sport": "nba", "card_date": day, "shadow": "yes",
                       "shadow_label": "LEAN", "shadow_stake": "0.5"})
            if i < 3:
                t3.append({"pick_id": pid, "sport": "nba", "card_date": day, "og3": "yes",
                           "og3_label": "LEAN", "og3_stake": "0.5", "rank": str(i + 1),
                           "price": "-110"})
    w("picks.csv", PICK_FIELDS, picks)
    w("results.csv", RESULT_FIELDS, res)
    w("shadow_card.csv", SHADOW_FIELDS, sh)
    w("og_top3.csv", OR.TOP3_FIELDS, t3)
    w("card_days.csv", CARD_DAY_FIELDS, days)


def test_review_report_side_by_side(tmp_path):
    from betdata import og_review as OGR

    (tmp_path / "ledger").mkdir()
    _ledger_og(tmp_path / "ledger", 4)
    md = OGR.report(tmp_path, NOW + timedelta(days=4))
    assert "| Record (W-L-P) | 12-8-0 | 8-4-0 |" in md  # top 5 vs top 3 (i = 0, 2 win)
    assert "| Picks per day | 5.0 (20 over 4 days) | 3.0 (12 over 4 days) |" in md
    assert "Worst 3-day stretch" in md and "Backtest data not found." in md
    assert OGR.write(tmp_path, NOW).read_text().startswith("# OG Ranking v2")


def test_worst_three_day_stretch():
    from betdata import og_review as OGR

    def row(day, res):
        return {"day": day, "result": res, "profit": "1.0" if res == "win" else "-1.0",
                "card_stake": "0.5", "odds": "100", "clv_consensus": ""}

    days = ["2026-11-12", "2026-11-13", "2026-11-14", "2026-11-15"]
    rows = [row(days[0], "win"), row(days[1], "loss"), row(days[2], "loss"),
            row(days[3], "loss")]
    assert OGR.worst_stretch(rows, days) == ("2026-11-13 to 2026-11-15", "0-3", -1.5)


def test_milestone_at_50_graded_or_nov_26_once(tmp_path):
    from betdata import og_review as OGR

    _ledger_og(tmp_path, 9)  # 45 graded
    now = datetime(2026, 11, 21, 12, tzinfo=UTC)
    assert OGR.milestone(tmp_path, now) == []
    _ledger_og(tmp_path, 10)  # 50 graded
    (m,) = OGR.milestone(tmp_path, now)
    assert (m["signal_name"], m["graded"]) == ("og-ranking-v2", "50")
    assert OGR.milestone(tmp_path, now) == []  # once
    other = tmp_path / "o"
    other.mkdir()
    _ledger_og(other, 2)  # 10 graded, but Nov 26
    (m2,) = OGR.milestone(other, datetime(2026, 11, 26, 12, tzinfo=UTC))
    assert m2["graded"] == "10"
    assert OGR.ntfy_line(other).startswith("OG Ranking v2 review ready: official 6-4-0")


def test_morning_dry_run_all_cards_and_top3_one_push(pdb, tmp_path, monkeypatch):
    """Next Gen, OG (Ranking v2), Spread Picks, the parlay and the OG Top 3 shadow chosen in one
    morning run, then ONE NBA push (the Top 3 never in it)."""
    import functools

    from betdata import morning as M
    from betdata import spread as SP
    from betdata.alerts.db import get_state

    with pdb.begin() as p:
        for i in range(4):
            _pick(p, f"og{i}", fair=0.7 - i / 100)
        _pick(p, "eng", signal="engine", market="h2h", side="home", line=None, price=250,
              median=None, fair=0.4, details={"ev": 0.4, "theta": 0.10, "p_market": 0.28})
        p.execute(insert(quotes).values(polled_at=NOW - timedelta(hours=3), sport="nba",
                                        game_id=7, source="sharpapi", book="draftkings",
                                        market="spreads", outcome="home", line=7.5,
                                        price=-110))  # fmt: skip
    tip = lambda m, s, d: TIP if s == "nba" else None  # noqa: E731
    run_ng, run_og = CD.run, CD.run_shadow
    monkeypatch.setattr(CD, "run", functools.partial(run_ng, tip_fn=tip))
    monkeypatch.setattr(CD, "run_shadow", functools.partial(run_og, tip_fn=tip))
    monkeypatch.setattr(CD, "in_ledger", lambda *a: False)
    monkeypatch.setattr(CD, "shadow_in_ledger", lambda *a: False)
    monkeypatch.setattr(SP, "in_ledger", lambda *a, **k: False)
    monkeypatch.setattr(M, "sports_due", lambda main, now: ["nba"])
    monkeypatch.setattr(M, "grade", lambda *a: None)
    monkeypatch.setattr(M, "engine", lambda *a: 1)
    monkeypatch.setattr(M, "ledger_sync", lambda *a: {})
    monkeypatch.setattr(M, "parlay_select", lambda *a: {"none": True})
    rep = M.run(_Main(), pdb, None, NOW, out=tmp_path, build=lambda: None)
    assert rep.errors == [], rep.errors
    assert rep.cards == {"nba": 1} and rep.shadow == {"nba": 4} and rep.spread == {"nba": 1}
    assert rep.og3 == {"nba": 3} and rep.parlay == "none"
    with pdb.connect() as p:
        og = get_state(p, CD.shadow_key("nba", NOW.date()))
        t3 = get_state(p, OR.top3_key("nba", NOW.date()))
    assert og["ranking"] == "v2" and [x["dedup_key"] for x in og["picks"]] == [
        "og0", "og1", "og2", "og3"]
    assert [x["dedup_key"] for x in t3["picks"]] == ["og0", "og1", "og2"]
    sent = []

    class Client:
        def post(self, url, content, headers):
            sent.append((headers["Title"], content.decode()))
            return type("R", (), {"status_code": 200})()

    monkeypatch.setattr("betdata.official.dataset", lambda out: [])
    monkeypatch.setattr("betdata.og_review.write", lambda root, now: None)
    assert M.push(pdb, tmp_path, NOW, "topic", Client()) == {"nba": "http 200"}
    ((title, body),) = sent  # one push
    assert title == "Morning update - NBA - Thu Nov 12"
    assert "OG:" in body and "Next Gen:" in body and "Spread picks: 1 —" in body
    assert "Top 3" not in body and "top 3" not in body
