"""M19 official record (betdata/official.py, MODEL.md "M19"): graded picks only, with a
reconciliation line; every total equals one computed independently from the ledger CSVs, on a
fixture day and on the real ledger in the repo."""

import csv
from datetime import date
from pathlib import Path

import pytest

from betdata import official as O
from betdata.ledger import CARD_FIELDS, PICK_FIELDS, RESULT_FIELDS, append_rows

ROOT = Path(__file__).resolve().parents[1]


def _ledger(out, picks):
    """picks: (id, day, signal, odds, result, profit, clv, card_stake or None)."""
    P, R, C = [], [], []
    for pid, day, sig, odds, res, profit, clv, stake in picks:
        P.append({"pick_id": pid, "dedup_key": pid, "created_at": f"{day}T14:00:00+00:00",
                  "sport": "nba", "game_id": 1, "game": "NYK @ BOS",
                  "tip": f"{day}T23:30:00+00:00", "market": "assists", "player": "A Player",
                  "side": "over", "line": "5.5", "odds": str(odds), "book": "fanduel",
                  "signal": sig, "signal_name": {"snipe": "F", "g2": "G2"}[sig]})
        if res:
            R.append({"pick_id": pid, "result": res, "profit": str(profit),
                      "clv_consensus": "" if clv is None else str(clv)})
        C.append({"pick_id": pid, "sport": "nba", "card_date": day,
                  "card": "yes" if stake else "no", "card_label": "LEAN" if stake else "",
                  "card_stake": "" if not stake else str(stake)})
    append_rows(out / "picks.csv", PICK_FIELDS, P)
    append_rows(out / "results.csv", RESULT_FIELDS, R)
    append_rows(out / "card.csv", CARD_FIELDS, C)


DAY = [("w", "2026-11-01", "snipe", 120, "win", 1.2, 0.02, 0.5),
       ("l", "2026-11-01", "snipe", -110, "loss", -1.0, -0.01, 0.5),
       ("v", "2026-11-01", "snipe", 105, "void", 0.0, None, 0.5),
       ("p", "2026-11-01", "snipe", 140, "", None, None, 0.5),
       ("x", "2026-11-01", "g2", -1200, "win", 0.0833, 0.01, None)]  # not on the card


def test_a_day_with_a_win_a_loss_a_void_and_a_pending_pick(tmp_path):
    _ledger(tmp_path, DAY)
    rows = O.dataset(tmp_path)
    rec = O.record(O.official(rows), O.official_stake)
    assert rec.line() == "4 picks = 2 graded (1-1-0) + 1 void + 1 pending"
    assert rec.units == pytest.approx(0.5 * 1.2 - 0.5)  # void and pending never count
    assert rec.staked == 1.0 and rec.roi == pytest.approx(0.1)
    assert rec.clv == pytest.approx(0.5)  # (2 - 1) / 2 pp
    assert rec.avg_odds == O.american((2.2 + 1 + 100 / 110) / 2)
    p = O.periods(O.official(rows), date(2026, 11, 2))
    assert p["Yesterday"].line() == p["All time"].line() == rec.line()
    sig = O.signals(rows)
    assert sig["G2"].line() == "1 pick = 1 graded (1-0-0) + 0 void + 0 pending"
    assert sig["F"].units == pytest.approx(1.2 - 1.0)  # research: flat 1 unit
    text = O.csv_text(O.by_day(rows)["2026-11-01"])
    got = list(csv.DictReader(text.splitlines()))
    assert list(got[0]) == list(O.CSV_FIELDS)
    assert {r["pick_id"]: (r["card"], r["result"], r["units"]) for r in got} == {
        "w": ("yes", "win", "0.6"), "l": ("yes", "loss", "-0.5"), "v": ("yes", "void", ""),
        "p": ("yes", "pending", ""), "x": ("no", "win", "0.0833")}


# --- an independent recount from the raw CSVs ------------------------------------------------


def _independent(ledger: Path):
    """Totals straight from the CSV files, without betdata.official or betdata.ledger."""
    def read(name):
        p = ledger / name
        return list(csv.DictReader(p.open())) if p.exists() else []

    excluded = {a["pick_id"] for a in read("annotations.csv")
                if (a["field"] == "signal_name" and a["value"] == "m16-legacy")
                or (a["field"] == "duplicate" and a["value"] == "true")}
    res = {r["pick_id"]: r for r in read("results.csv")}
    card = {r["pick_id"]: r for r in read("card.csv")}
    out = []
    for p in read("picks.csv"):
        if p["pick_id"] in excluded or p["signal"] == "news":
            continue
        r, c = res.get(p["pick_id"], {}), card.get(p["pick_id"], {})
        out.append((p, r, c))
    return out


def _tally(items, stake):
    n = w = l_ = pu = v = pend = 0
    units = 0.0
    for _p, r, c in items:
        n += 1
        res = r.get("result", "")
        if res == "void":
            v += 1
        elif res not in ("win", "loss", "push"):
            pend += 1
        else:
            w += res == "win"
            l_ += res == "loss"
            pu += res == "push"
            units += stake(c) * float(r["profit"])
    return n, w, l_, pu, v, pend, round(units, 6)


def _ours(rec):
    return (rec.picks, rec.wins, rec.losses, rec.pushes, rec.voids, rec.pending,
            round(rec.units, 6))


@pytest.mark.parametrize("where", ["fixture", "repo"])
def test_page_totals_equal_an_independent_count_of_the_csv(where, tmp_path):
    ledger = tmp_path if where == "fixture" else ROOT / "ledger"
    if where == "fixture":
        _ledger(tmp_path, DAY)
    elif not (ledger / "card.csv").exists():
        pytest.skip("no ledger")
    rows = O.dataset(ledger)
    items = _independent(ledger)
    card = [x for x in items if x[2].get("card") == "yes"]
    stake = lambda c: float(c["card_stake"])  # noqa: E731
    # official all time
    assert _ours(O.record(O.official(rows), O.official_stake)) == _tally(card, stake)
    # three days (or all there are), newest first
    days = sorted({x[2]["card_date"] for x in card}, reverse=True)[:3]
    for d in days:
        assert _ours(O.record([r for r in O.official(rows) if r["day"] == d],
                              O.official_stake)) == _tally(
            [x for x in card if x[2]["card_date"] == d], stake)  # fmt: skip
    # each signal (flat 1 unit)
    for name, rec in O.signals(rows).items():
        assert _ours(rec) == _tally([x for x in items if x[0]["signal_name"] == name],
                                    lambda c: 1.0)  # fmt: skip
