"""M20 B1/B3: stints-derived facts and the no-market rule."""

import pandas as pd

from betdata.nba.engine import derive as D


def test_running_margin_and_noncompetitive_definitions():
    st = pd.DataFrame({"game_id": 1, "period": [1, 2, 3, 4, 4], "start_sec":
                       [0, 720, 1440, 2160, 2600], "end_sec": [720, 1440, 2160, 2600, 2880],
                       "home_players": "a", "away_players": "b",
                       "home_pts": [30, 30, 30, 20, 5], "away_pts": [20, 20, 20, 15, 5],
                       "home_poss": 1.0, "away_poss": 1.0})  # fmt: skip
    s = D.with_margin(st)
    assert s.margin.tolist() == [0, 10, 20, 30, 35]
    nc = D.noncompetitive(s, 13.0)
    assert nc.cm1.tolist() == [False, False, False, False, True]  # last 5:00, up 35
    assert nc.cm3.tolist() == [False, False, False, True, True]  # Q4, up 20+
    assert s.complete.all()


def test_incomplete_game_has_no_noncompetitive_time():
    st = pd.DataFrame({"game_id": 1, "period": [1, 3], "start_sec": [0, 1440],
                       "end_sec": [720, 2160], "home_players": "a", "away_players": "b",
                       "home_pts": [40, 40], "away_pts": [0, 0], "home_poss": 1.0,
                       "away_poss": 1.0})  # fmt: skip
    s = D.with_margin(st)
    assert not s.complete.any()
    assert not D.noncompetitive(s).any().any()


def test_engine_package_reads_no_line_table():
    import pathlib

    root = pathlib.Path(D.__file__).parent
    for f in root.glob("[!_]*.py"):
        if f.name in ("blend.py", "picks.py", "live.py", "evaluate.py", "page.py"):
            continue  # these take market prices after the engine has predicted
        text = f.read_text()
        for bad in ("odds_snapshots", "prop_lines", "quotes", "consensus", "load_odds"):
            assert bad not in text, f"{f.name} reads {bad}"


