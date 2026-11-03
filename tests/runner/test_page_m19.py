"""M19 page sections (page/official.py; MODEL.md "M19: Page layout"): the official record header,
results by day and the Research tab, built from a fixture ledger; every number on the page
equals an independent recount of the CSVs, and every record links to the CSV of its picks."""

import csv
import re
from datetime import date

from betdata import official as O
from betdata.ledger import CARD_FIELDS, PICK_FIELDS, RESULT_FIELDS, append_rows
from betdata.page.build import et
from betdata.page.official import days_html, header_html, research_html
from betdata.review import PICK_FIELDS as RP
from betdata.review import RESULT_FIELDS as RR

TODAY = date(2026, 11, 2)
PICKS = [  # id, day, player, market, side, line, odds, result, profit, clv, card stake, tag
    ("w", "2026-11-01", "Anthony Black", "assists", "under", 3.5, 120, "win", 1.2, 0.02, 0.5,
     "Variance"),
    ("l", "2026-11-01", "Paolo Banchero", "rebounds", "over", 8.5, -110, "loss", -1.0, -0.01,
     0.5, "Minutes miss"),
    ("v", "2026-11-01", "Jalen Suggs", "points", "over", 14.5, 105, "void", 0.0, None, 0.5, ""),
    ("p", "2026-11-02", "Bam Adebayo", "rebounds", "over", 9.5, 140, "", None, None, 0.5, ""),
    ("o", "2026-10-31", "Tyler Herro", "assists", "over", 4.5, -105, "loss", -1.0, 0.01, 1.0,
     "News after pick"),
    ("x", "2026-11-01", "Kel'el Ware", "rebounds", "under", 7.5, -120, "win", 0.833, 0.01, None,
     "Variance"),  # research only
]


def _ledger(out):
    P, R, C, A, B = [], [], [], [], []
    for pid, day, player, market, side, line, odds, res, profit, clv, stake, tag in PICKS:
        P.append({"pick_id": pid, "dedup_key": pid, "created_at": f"{day}T14:00:00+00:00",
                  "sport": "nba", "game_id": 1, "game": "ORL @ MIA",
                  "tip": f"{day}T23:30:00+00:00", "market": market, "player": player,
                  "side": side, "line": str(line), "odds": str(odds), "book": "fanduel",
                  "signal": "snipe", "signal_name": "F"})
        if res:
            R.append({"pick_id": pid, "result": res, "profit": str(profit),
                      "clv_consensus": "" if clv is None else str(clv)})
            B.append({"pick_id": pid, "actual_stat": "2.0", "tag": tag})
        C.append({"pick_id": pid, "sport": "nba", "card_date": day,
                  "card": "yes" if stake else "no", "card_label": "LEAN" if stake else "",
                  "card_stake": "" if not stake else str(stake)})
        A.append({"pick_id": pid, "team": "MIA" if player in ("Bam Adebayo", "Tyler Herro",
                                                              "Kel'el Ware") else "ORL",
                  "opponent": "ORL" if player in ("Bam Adebayo", "Tyler Herro",
                                                  "Kel'el Ware") else "MIA"})
    append_rows(out / "picks.csv", PICK_FIELDS, P)
    append_rows(out / "results.csv", RESULT_FIELDS, R)
    append_rows(out / "card.csv", CARD_FIELDS, C)
    append_rows(out / "review_pick.csv", RP, A)
    append_rows(out / "review_result.csv", RR, B)


def _recs(html: str) -> list[dict]:
    return [dict(re.findall(r"data-([a-z]+)='([^']*)'", m)) for m in
            re.findall(r"<span class='rec' ([^>]*)>", html)]


def test_header_numbers_equal_an_independent_recount(tmp_path):
    led, site = tmp_path / "ledger", tmp_path / "site"
    _ledger(led)
    rows = O.dataset(led)
    html = header_html(rows, "nba", "NBA", TODAY, site)
    alltime, week, yest = _recs(html)
    # independently, from the CSVs: card picks, graded only, card stakes
    card = {r["pick_id"]: r for r in csv.DictReader((led / "card.csv").open())
            if r["card"] == "yes"}
    res = {r["pick_id"]: r for r in csv.DictReader((led / "results.csv").open())}
    w = sum(1 for k in card if res.get(k, {}).get("result") == "win")
    lo = sum(1 for k in card if res.get(k, {}).get("result") == "loss")
    units = sum(float(card[k]["card_stake"]) * float(res[k]["profit"]) for k in card
                if res.get(k, {}).get("result") in ("win", "loss", "push"))
    assert (int(alltime["w"]), int(alltime["l"]), int(alltime["picks"])) == (w, lo, len(card))
    assert float(alltime["units"]) == round(units, 4)
    assert (alltime["void"], alltime["pending"]) == ("1", "1")
    assert "5 picks = 3 graded (1-2-0) + 1 void + 1 pending" in html
    assert (yest["w"], yest["l"], yest["void"], yest["pending"]) == ("1", "1", "1", "0")
    assert week["picks"] == "5"
    # every record links to the CSV of exactly its picks
    for href in re.findall(r"href='([^']+)'", html):
        got = list(csv.DictReader((site / href).open()))
        assert all(r["card"] == "yes" for r in got)
    yest_csv = list(csv.DictReader((site / "csv/nba-official-yesterday.csv").open()))
    assert sorted(r["pick_id"] for r in yest_csv) == ["l", "v", "w"]


def test_results_by_day_open_to_every_card_pick_with_stat_and_tag(tmp_path):
    led, site = tmp_path / "ledger", tmp_path / "site"
    _ledger(led)
    html = days_html(O.dataset(led), "nba", TODAY, site, et)
    days = re.findall(r"data-day='([\d-]+)'", html)
    assert days == ["2026-11-02", "2026-11-01", "2026-10-31"]  # newest first
    nov1 = html[html.index("data-day='2026-11-01'"):html.index("data-day='2026-10-31'")]
    assert "3 picks = 2 graded (1-1-0) + 1 void + 0 pending" in nov1
    assert "Anthony Black (ORL) vs MIA" in nov1 and "why: <b>Minutes miss</b>" in nov1
    assert "WIN +0.60u</span> · actual 2.0" in nov1 and "VOID (did not play)" in nov1
    assert "Losses (1): 1 Minutes miss · Wins (1): 1 Variance" in nov1
    assert "Kel'el Ware" not in nov1  # research picks are not on the day's card list
    day_csv = list(csv.DictReader((site / "csv/nba-day-2026-11-01.csv").open()))
    assert list(day_csv[0]) == list(O.CSV_FIELDS)
    assert {r["pick_id"]: r["card"] for r in day_csv} == {"w": "yes", "l": "yes", "v": "yes",
                                                          "x": "no"}  # every pick of the day
    assert "Last 7 days, why picks won or lost: Losses (2): 1 Minutes miss, 1 News after pick" \
        in html
    assert "type='date' class='dayjump'" in html


def test_research_tab_has_every_signal_with_units_and_gate(tmp_path):
    led, site = tmp_path / "ledger", tmp_path / "site"
    _ledger(led)
    html = research_html(O.dataset(led), site)
    [f] = _recs(html)
    assert (f["picks"], f["w"], f["l"], f["void"], f["pending"]) == ("6", "2", "2", "1", "1")
    assert float(f["units"]) == round(1.2 - 1.0 - 1.0 + 0.833, 4)  # flat 1 unit
    assert "Gate: 4/200 graded with CLV" in html and "research, not official picks" in html
