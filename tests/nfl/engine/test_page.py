"""M17 page: the Engine vs market section and the engine's season record."""

from datetime import UTC, datetime

import numpy as np

from betdata.nfl.engine.live import EngineDesign
from betdata.nfl.engine.page import p_cover, section_html
from betdata.page.build import et, records_html
from betdata.quotes import Quote
from betdata.sports import RecordRow

NOW = datetime(2026, 11, 1, 17, 0, tzinfo=UTC)


def _hist(mean=5.0):
    h = np.zeros(121, int)
    for m in range(-30, 40):
        h[m + 60] = int(1000 * np.exp(-((m - mean) ** 2) / 200))
    return h.tolist()


def _row(margin=5.0, line=-1.0):
    return {"game_id": 1, "home": "KC", "away": "BUF", "start_time_utc": NOW,
            "pred_home_margin": margin, "pred_total": 47.3, "prediction_point": "final_report",
            "predicted_at": NOW,
            "params": {"margin_p10": -8.0, "margin_p90": 18.0, "total_p10": 33.0,
                       "total_p90": 62.0, "margin_hist": _hist(margin)},
            "factors": {"steps": {"pass_offence": {"margin": 2.1, "total": 1.0},
                                  "run_defence": {"margin": -0.6, "total": 0.2},
                                  "home": {"margin": 1.4, "total": 0.0},
                                  "pace": {"margin": 0.05, "total": 0.3}}},
            "spread_now": Quote(line, -110, "draftkings", "sharpapi", NOW),
            "total_now": Quote(46.5, -110, "draftkings", "sharpapi", NOW)}


def test_engine_section_shows_gap_cover_steps_and_flags_only_past_the_threshold():
    d = EngineDesign(fit="x", spread_threshold=4.0)
    html = section_html([_row(5.0, -1.0)], {}, d, et)
    assert "Engine vs market" in html and "market-free" in html
    assert "Engine <b>KC −5.0</b>" in html and "market KC −1.0" in html
    assert "gap 4.0 toward KC" in html and "P(cover KC)" in html
    assert '<span class="flag">Engine: KC -1</span>' in html
    assert "passing offence KC +2.1" in html and "home field KC +1.4" in html
    assert "pace" not in html.split("Biggest steps:")[1].split("</div>")[0]
    quiet = section_html([_row(2.0, -1.0)], {}, d, et)
    assert 'class="flag"' not in quiet
    none = section_html([_row(9.0, -1.0)], {}, EngineDesign(fit="x"), et)
    assert 'class="flag"' not in none and "No side flags" in none


def test_props_are_info_only_without_an_earned_threshold():
    d = EngineDesign(fit="x", spread_threshold=None, prop_thresholds={"rec_yds": 0.08})
    pr = {1: [{"player_name": "Travis Kelce", "market": "rec_yds", "mean": 58.0, "line": 52.5,
               "p_over": 0.61, "edge": 9.5},
              {"player_name": "Isiah Pacheco", "market": "rush_yds", "mean": 61.0,
               "line": 64.5, "p_over": 0.45, "edge": -3.0}]}
    html = section_html([_row()], pr, d, et)
    assert "Travis Kelce · rec yds: median <b>58.0</b>" in html
    assert '<span class="flag">Over 52.5</span>' in html
    assert html.count("info only") == 1  # rush yds has no earned threshold


def test_p_cover_matches_the_histogram():
    assert abs(p_cover(_hist(0.0), -0.5) - 0.5) < 0.05
    assert p_cover(_hist(10.0), -3.5) > 0.6


def test_engine_record_sits_beside_the_page_record():
    rows = [RecordRow("spread", "m13w", 30, 25, 1, 0, 2.5, 0.4, 0.55, "pts"),
            RecordRow("spread", "nfle17:abc", 12, 9, 0, 0, 2.1, 0.3, 0.6, "pts"),
            RecordRow("spread", "m15:def", 20, 22, 0, 0, -3.0, None, None, "pts")]
    html = records_html(rows, "m13w", engine_version="nfle17")
    head, other = html.split("Other model versions")
    assert "Engine (market-free) nfle17:abc" in head
    assert "nfle17" not in other and "m15:def" in other
