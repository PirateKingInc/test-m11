"""The spread engine (owner 2026-11-14, nba/engine/spread.py): recalibrated engine margin, P(cover),
walk-forward validation, the pre-registered T and the go-live bar."""

import numpy as np
import pandas as pd

from betdata.nba.engine import spread as S


def _games(seed=0, n=600, seasons=range(2016, 2026), shrink=0.4, edge=0.0):
    """Synthetic seasons: true margin m ~ N(market + edge-ish, 12); the engine sees a shrunk m."""
    rng = np.random.default_rng(seed)
    rows = []
    for s in seasons:
        mkt = rng.normal(2.5, 6.5, n)
        true = mkt + edge * rng.normal(0, 3, n)
        margin = np.round(true + rng.normal(0, 12, n))
        mu = 0.6 + shrink * true + rng.normal(0, 0.5, n)
        rows.append(pd.DataFrame({"season": s, "mu": mu, "sd": 16.0, "margin": margin,
                                  "c_spread": -np.round(mkt * 2) / 2,
                                  "o_spread": -np.round(mkt * 2) / 2,
                                  "c_ml_home": np.where(mkt > 0, -150, 130),
                                  "c_ml_away": np.where(mkt > 0, 130, -150),
                                  "p_home_win": 0.5 + 0.02 * mu}))  # fmt: skip
    return pd.concat(rows, ignore_index=True)


def test_fit_undoes_the_shrink_and_sigma_matches_the_residual():
    d = _games()
    m = S.SpreadModel.fit(d)
    assert 2.2 < m.b < 2.8  # 1 / 0.4: the engine's margin de-shrunk
    assert 11.0 < m.sigma < 13.5
    assert abs(float(np.mean(m.margin(d.mu) - d.margin))) < 0.2


def test_p_cover_symmetry_and_half_point_lines():
    m = S.SpreadModel(a=0.0, b=1.0, sigma=12.0)
    assert abs(float(m.p_home_cover(0.0, 0.0)) - 0.5) < 1e-12
    p = float(m.p_home_cover(3.0, -1.5))  # home by 3 expected, gives 1.5
    q = float(m.p_home_cover(-3.0, 1.5))  # mirror: the away view
    assert abs(p - (1 - q)) < 1e-12 and p > 0.5  # the mirrored game: 1 - p


def test_walk_forward_and_test_never_fit_on_later_or_test_seasons():
    d = _games()
    v = S.walk_forward(d)
    for s, g in v.groupby("season"):
        m = g["_m"].iloc[0]
        assert max(m.seasons) < s and min(m.seasons) >= S.FIRST
        assert S.leakage_ok(m)
    t, table = S.choose_t(d)
    assert t in S.T_GRID and len(table) == len(S.T_GRID)
    res = S.test(d, t)
    assert max(res["model"]["seasons"]) == 2022 and res["line"] == "opening"
    assert not set(res["model"]["seasons"]) & set(S.TEST)


def test_choose_t_reads_validation_seasons_only():
    d = _games()
    t1, _ = S.choose_t(d)
    d2 = d.copy()
    d2.loc[d2.season.isin(S.TEST), "margin"] = 999  # scramble the test seasons
    t2, _ = S.choose_t(d2)
    assert t1 == t2


def test_picks_dogs_only_results_and_bar():
    d = pd.DataFrame({"season": 2023, "mu": [10.0, -10.0, 0.0], "margin": [5, -20, 3],
                      "o_spread": [3.0, -4.0, 1.0]})  # fmt: skip
    m = S.SpreadModel(a=0.0, b=1.0, sigma=12.0)
    g = S.picks(d, m, "o_spread", 0.55)
    # game 1: home +3 (dog) expected +10: home covers 5+3>0 win; game 2: away +4 is the dog,
    # expected -10 (away by 10): away covers -( -20 - 4) > 0 win; game 3: below T
    assert list(g.side) == ["home", "away"] and list(g.result) == ["win", "win"]
    assert S.record(g)["units"] == round(2 * S.WIN_AT_110, 2)
    res = {"dogs": {"n": 320, "win": 0.55, "win_95": [0.51, 0.6]},
           "by_season": {s: {"dogs": {"units": 1.0 if s != 2025 else -1.0}} for s in S.TEST},
           "engine_vs_market_slope": 1.0}
    assert S.bar(res)["met"]
    res["engine_vs_market_slope"] = 0.4
    b = S.bar(res)
    assert not b["met"] and not b["checks"]["engine-vs-market slope 0.8-1.25"]["ok"]


def test_diagnose_reports_the_squeeze():
    d = _games()
    r = S.diagnose(d)
    assert r["actual_on_engine_slope"] > 2 and r["engine_on_market_slope"] < 0.5
    assert 0.8 < r["engine_vs_market_slope_after"] < 1.25


def test_live_section_and_report_md():
    grades = [{"kind": "game", "market": "spreads", "line": "5.5", "p_engine": "0.62",
               "result": "win"},  # home dog taken, covered
              {"kind": "game", "market": "spreads", "line": "-4.5", "p_engine": "0.30",
               "result": "win"},  # away dog at 0.70 taken; home covered: a loss
              {"kind": "game", "market": "spreads", "line": "-3.5", "p_engine": "0.55",
               "result": "loss"}]  # the favourite side: never a pick
    live = S.live_section(grades, 0.56)
    assert (live["record"]["w"], live["record"]["l"]) == (1, 1)
    d = _games()
    t, table = S.choose_t(d[d.season <= 2022])
    valid = {"diagnosis_2016_2022": S.diagnose(d[d.season <= 2022]), "t": t,
             "validation_table": table}
    res = S.test(d, t)
    res["bar"], res["d5"] = S.bar(res), S.d5(d)
    md = S.report_md(valid, res, live, "2026-11-14")
    assert "## D1" in md and f"T = {t}" in md and "## D4" in md and "## D5" in md
    assert "Live season (labelled)" in md
