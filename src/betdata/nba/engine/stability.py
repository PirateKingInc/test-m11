"""M20 B2: the stability table (MODEL.md "M20 B2"), training seasons only.

For every rate: split-half reliability r (odd vs even games within a season), the median
opportunities per half n, the empirical padding k = n (1 - r) / r, and the season-to-season
reliability. An estimate after m opportunities is (m x + k prior) / (m + k).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from betdata.nba.engine.data import PCTS, RATES

MIN_HALF_GAMES = 10
MIN_SEASON_POSS = 500.0


def _corr(a: pd.Series, b: pd.Series) -> float:
    ok = a.notna() & b.notna() & np.isfinite(a) & np.isfinite(b)
    return float(np.corrcoef(a[ok], b[ok])[0, 1]) if ok.sum() > 10 else float("nan")


def _padding(r: float, n: float) -> float:
    return float(n * (1 - r) / r) if r > 0 else float("inf")


def player_specs(cm: str) -> list[tuple[str, str, str, str]]:
    """(name, numerator, denominator, unit): rates per on-court possession, percentages per
    attempt, minutes shares per game."""
    out = [(r, r, "poss_on", "possessions") for r in RATES]
    out.append(("usage", "_use", "poss_on", "possessions"))
    out += [(f"{k}_pct", m, a, "attempts") for k, (m, a) in PCTS.items()]
    out += [("comp_share", f"c_{cm}", "_one", "games"),
            ("garbage_share", f"g_{cm}", "_one", "games")]
    return out


def team_specs() -> list[tuple[str, str, str, str]]:
    return [("pace", "_pace", "_one", "games"),
            ("off_ppp", "pts", "poss", "possessions"),
            ("def_ppp", "o_pts", "o_poss", "possessions"),
            ("oreb_rate", "oreb", "_oreb_ch", "chances"),
            ("dreb_rate", "dreb", "_dreb_ch", "chances"),
            ("fg3a_allowed", "o_fg3a", "o_poss", "possessions"),
            ("fg2_pct_allowed", "o_fg2m", "o_fg2a", "attempts"),
            ("fg3_pct_allowed", "o_fg3m", "o_fg3a", "attempts"),
            ("tov_forced", "o_tov", "o_poss", "possessions"),
            ("ast_share", "ast", "fgm", "made FG")]


def _table(d: pd.DataFrame, unit_col: str, specs, min_half: int, min_season: float,
           level: str) -> list[dict]:  # fmt: skip
    d = d.sort_values("tip").copy()
    d["_k"] = d.groupby([unit_col, "season"]).cumcount() % 2
    d["_one"] = 1.0
    rows = []
    for name, num, den, unit in specs:
        x = d[d[num].notna() & d[den].notna()]
        h = x.groupby([unit_col, "season", "_k"]).agg(n=(num, "sum"), m=(den, "sum"),
                                                     g=(num, "size")).unstack("_k")
        h = h[(h["g"][0] >= min_half) & (h["g"][1] >= min_half)]
        r0, r1 = h["n"][0] / h["m"][0], h["n"][1] / h["m"][1]
        r = _corr(r0, r1)
        n_half = float(np.median(np.concatenate([h["m"][0], h["m"][1]]))) if len(h) else np.nan
        s = x.groupby([unit_col, "season"]).agg(n=(num, "sum"), m=(den, "sum")).reset_index()
        if unit == "possessions":
            s = s[s.m >= min_season] if level == "player" else s
        s["rate"] = s.n / s.m
        nxt = s.assign(season=s.season - 1)[[unit_col, "season", "rate"]]
        ss = s.merge(nxt, on=[unit_col, "season"], suffixes=("", "_next"))
        league = float(x[num].sum() / x[den].sum())
        rows.append({"level": level, "rate": name, "unit": unit, "league": round(league, 4),
                     "units": int(len(h)), "split_half_r": round(r, 3),
                     "spearman_brown_r": round(2 * r / (1 + r), 3) if r > -1 else np.nan,
                     "median_half_n": round(n_half, 1),
                     "padding": round(_padding(r, n_half), 1),
                     "season_to_season_r": round(_corr(ss.rate, ss.rate_next), 3),
                     "season_pairs": int(len(ss))})
    return rows


def stability(p: pd.DataFrame, tg: pd.DataFrame, seasons, cm: str = "cm1") -> pd.DataFrame:
    """The table for player-games `p` and team-games `tg` of `seasons`."""
    seasons = list(seasons)
    pl = p[p.played & p.season.isin(seasons)].copy()
    pl["_use"] = pl.fga + 0.44 * pl.fta + pl.tov
    rows = _table(pl, "player_id", player_specs(cm), MIN_HALF_GAMES, MIN_SEASON_POSS, "player")
    t = tg[tg.season.isin(seasons)].copy()
    t["_pace"] = 48 * t.poss / t.game_min
    t["_oreb_ch"] = t.oreb + t.o_dreb
    t["_dreb_ch"] = t.dreb + t.o_oreb
    t["o_fg2m"] = t.o_fgm - t.o_fg3m
    t["o_fg2a"] = t.o_fga - t.o_fg3a
    rows += _table(t, "team_id", team_specs(), MIN_HALF_GAMES, 0.0, "team")
    return pd.DataFrame(rows)


def paddings(table: pd.DataFrame) -> dict[str, dict[str, float]]:
    """{rate: {padding, season_r, league}} for the engine (players and teams by level)."""
    out = {}
    for r in table.itertuples():
        out[f"{r.level}:{r.rate}"] = {"padding": float(r.padding),
                                      "season_r": float(r.season_to_season_r),
                                      "league": float(r.league)}
    return out
