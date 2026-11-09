"""Rules v2 diagnostics (reporting only): the ledger half runs on a ledger and the report
renders, with or without the database half."""

from betdata import diagnostics as D


def test_engine_gap_and_report():
    rows = [{"market": "assists", "kind": "prop", "p_engine": "0.70", "p_close": "0.55",
             "y": "1"},
            {"market": "assists", "kind": "prop", "p_engine": "0.30", "p_close": "0.45",
             "y": "1"},
            {"market": "points", "kind": "prop", "p_engine": "0.52", "p_close": "0.50",
             "y": "0"}]  # fmt: skip
    e = D.engine_part(rows)
    g = e["by_gap"]["8+ pts"]
    assert g["n"] == 2 and g["hit"] == 0.5  # the engine's side: hit once, missed once
    assert e["by_gap"]["0-4 pts"]["n"] == 1
    led = {"role": {"assists": {"teammates_out": {"2+": {"w": 1, "l": 2, "n": 3, "units": -1.1}},
                                "minutes_vs_projection": {}, "overshoot_rate": {
                                    "tm2": [None, 0], "tm01": [None, 0]}},
                    "rebounds": {"teammates_out": {}, "minutes_vs_projection": {},
                                 "overshoot_rate": {"tm2": [None, 0], "tm01": [None, 0]}}},
           "home_away": {"all": {"home": {"w": 0, "l": 0, "n": 0, "units": 0},
                                 "away": {"w": 0, "l": 0, "n": 0, "units": 0}}},
           "engine": e}
    md = D.report(led, None)
    assert md.startswith("# Rules v2 diagnostics (reporting only)")
    assert "## Summary" in md and "need the full database" in md
    db = {"6_rules_on_past_seasons_all": {"kept": {"w": 1, "l": 0, "units": 1.0, "roi": 1.0}}}
    assert "a check on rules chosen from 2026 data" in D.report(led, db)
