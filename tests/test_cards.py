"""Two official NBA cards (owner decision 2026-11-05, betdata/cards.py, config/cards.yaml):
"Next Gen" (the engine card, from Nov 5) and "OG" (Card v1's rule: its record continues from
Card v1's Oct 31 - Nov 4 picks, then every OG card from Nov 6; Nov 5 is excluded through the
configuration and an annotation, never by rewriting the ledger)."""

from datetime import UTC, datetime

from betdata import cards as C
from betdata import ledger as L
from betdata import official as O

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _ledger(out):
    """Card v1 (card.csv, Nov 3), Next Gen (card.csv, Nov 5) and OG (shadow_card.csv, Nov 5
    switch-over and Nov 6) picks, all graded."""
    P, R, Cd, S = [], [], [], []
    for pid, sig, day, profit in (("v1a", "F", "2026-11-03", 1.0), ("v1b", "C-NBA",
                                                                     "2026-11-03", -1.0),
                                  ("ng", "nba-engine", "2026-11-05", 0.9),
                                  ("og5", "F", "2026-11-05", 1.2), ("og6", "F", "2026-11-06",
                                                                    -1.0)):  # fmt: skip
        P.append({"pick_id": pid, "dedup_key": pid, "created_at": f"{day}T10:00:00+00:00",
                  "sport": "nba", "game_id": 1, "game": "ORL @ MIA",
                  "tip": f"{day}T23:30:00+00:00", "market": "points", "player": "Same Guy",
                  "side": "over", "line": "20.5", "odds": "100",
                  "signal": {"F": "snipe", "C-NBA": "value"}.get(sig, "engine"),
                  "signal_name": sig})
        R.append({"pick_id": pid, "result": "win" if profit > 0 else "loss",
                  "profit": str(profit), "clv_consensus": ""})
        if sig == "nba-engine" or day <= "2026-11-04":
            Cd.append({"pick_id": pid, "sport": "nba", "card_date": day, "card": "yes",
                       "card_label": "TAKE", "card_stake": "1.0"})
        else:
            S.append({"pick_id": pid, "sport": "nba", "card_date": day, "shadow": "yes",
                      "shadow_label": "LEAN", "shadow_stake": "0.5",
                      "signal_name": "card-v1-shadow"})
    L.append_rows(out / "picks.csv", L.PICK_FIELDS, P)
    L.append_rows(out / "results.csv", L.RESULT_FIELDS, R)
    L.append_rows(out / "card.csv", L.CARD_FIELDS, Cd)
    L.append_rows(out / "shadow_card.csv", L.SHADOW_FIELDS, S)


def test_config_names_and_rules():
    cfg = C.load()
    assert (cfg["nextgen"].name, cfg["og"].name) == ("Next Gen", "OG")
    assert cfg["og"].signals == ("value", "snipe") and cfg["nextgen"].signals == ("engine",)
    assert cfg["og"].exclude_days == ("2026-11-05",) and C.default() == "nextgen"


def test_og_record_continues_card_v1_and_excludes_nov_5(tmp_path):
    out = tmp_path / "ledger"
    out.mkdir()
    _ledger(out)
    before = (out / "shadow_card.csv").read_text()
    assert L.annotate(out, NOW) >= 1  # the switch-over pick is annotated, once
    assert L.annotate(out, NOW) == 0
    assert (out / "shadow_card.csv").read_text() == before  # never rewritten
    notes = [r for r in L.read_rows(out / "annotations.csv") if r["field"] == "og_excluded"]
    assert [(n["pick_id"], n["value"]) for n in notes] == [("og5", "true")]
    rows = O.dataset(out)
    og = C.view(rows, C.OG)
    assert sorted((r["pick_id"], r["day"]) for r in og) == [
        ("og6", "2026-11-06"), ("v1a", "2026-11-03"), ("v1b", "2026-11-03")]
    assert {r["card_stake"] for r in og if r["pick_id"] == "og6"} == {"0.5"}
    rec = O.record(og, O.official_stake)
    assert (rec.wins, rec.losses, round(rec.units, 2)) == (1, 2, -0.5)
    ng = C.view(rows, C.NEXTGEN)
    assert [r["pick_id"] for r in ng] == ["ng"]
    # each card's own goal
    assert O.goal(og).graded == 3 and O.goal(ng).graded == 1


def test_annotation_alone_excludes(tmp_path):
    rows = [{"pick_id": "x", "sport": "nba", "official": False, "signal_name": "F",
             "day": "2026-11-09", "shadow": True, "shadow_date": "2026-11-09",
             "shadow_stake": "1.0", "shadow_label": "TAKE", "og_excluded": "true"}]
    assert C.view(rows, C.OG) == []


def test_the_same_bet_on_both_cards_is_marked(tmp_path):
    rows = [{"pick_id": "a", "sport": "nba", "official": True, "signal_name": "nba-engine",
             "day": "2026-11-06", "game_id": 7, "market": "points", "player": "Same Guy",
             "side": "over"},
            {"pick_id": "b", "sport": "nba", "official": False, "signal_name": "F",
             "day": "2026-11-06", "game_id": 7, "market": "points", "player": "same guy",
             "side": "over", "shadow": True, "shadow_date": "2026-11-06",
             "shadow_stake": "0.5", "shadow_label": "LEAN"}]
    both = C.on_both(rows)
    assert both == {C.bet_key(7, "points", "Same Guy", "over")}
    assert all(C.both_mark(r, both) for r in rows)


def test_the_page_picks_csv_keeps_every_row_with_annotations(tmp_path, monkeypatch):
    """Regression (2026-11-06): an annotation field (og_excluded, news_status) is not a
    picks.csv column; the export must skip it, not stop at the first annotated pick."""
    import csv

    from betdata import ledger as LG
    from betdata.page.build import _ledger

    out = tmp_path / "ledger"
    out.mkdir()
    from tests.test_cards import _ledger as fill

    fill(out)
    assert LG.annotate(out, NOW) >= 1  # og5 gets og_excluded
    monkeypatch.setattr(LG, "ledger_dir", lambda: out)
    site = tmp_path / "site"
    rows = _ledger(site)
    assert rows is not None and len(rows) == 5
    got = list(csv.DictReader((site / "picks.csv").open()))
    assert sorted(r["pick_id"] for r in got) == ["ng", "og5", "og6", "v1a", "v1b"]
