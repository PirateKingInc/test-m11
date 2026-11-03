"""M20 Part D add-ons, each switchable and judged alone on validation (MODEL.md "M20: Part D").

D1  with/without key teammates: a player's per-possession rates in his games without each of
    his three most-shared teammates vs with them, shrunk by possessions apart (B2 padding),
    applied in proportion to the teammate's chance of sitting tonight. Stints carry no player
    stats, so "without" means the games the teammate missed (box scores), not minutes apart.
D2  rebounding environment: each player's share of the defensive rebounds moves with the
    share of the opponent's misses that are threes (long rebounds), by role, fitted on
    training seasons.
D3  assists x teammate shooting: a player's assists scale with his expected on-court
    teammates' shooting quality (shrunk effective FG%) relative to the league.
D4  game script into minutes: the core's E[non-competitive minutes | margin] term; D4 is
    tested by removing it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from betdata.nba.engine.data import RATES

D1_STATS = ("fg2a", "fg3a", "fta", "ast", "oreb", "dreb", "tov")
KEY_N = 3


# --- D1 -----------------------------------------------------------------------------------------

def key_teammates(p: pd.DataFrame) -> pd.DataFrame:
    """Per (player, season, team): his KEY_N teammates with the most shared games x minutes,
    from the games known so far (as-of: the pair list uses only earlier games via cumulative
    sums at lookup)."""
    pl = p[p.played][["game_id", "team_id", "season", "player_id", "minutes"]]
    pairs = pl.merge(pl, on=["game_id", "team_id", "season"], suffixes=("", "_j"))
    pairs = pairs[pairs.player_id != pairs.player_id_j]
    pairs["shared"] = np.minimum(pairs.minutes, pairs.minutes_j)
    s = pairs.groupby(["season", "team_id", "player_id", "player_id_j"]).shared.sum()
    s = s.reset_index().sort_values("shared", ascending=False)
    return s.groupby(["season", "team_id", "player_id"]).head(KEY_N)


def without_sums(p: pd.DataFrame, keys: pd.DataFrame) -> pd.DataFrame:
    """For each (player i, teammate j) and each of i's games: cumulative possessions and stat
    sums of i in this season's games with j absent, and with j present (known at known_at)."""
    pl = p[p.played].copy()
    tp = pl.groupby(["game_id", "team_id"]).player_id.apply(set)
    rows = pl.merge(keys[["season", "team_id", "player_id", "player_id_j"]],
                    on=["season", "team_id", "player_id"])  # fmt: skip
    present = [pj in tp.get((g, t), ()) for g, t, pj in zip(rows.game_id, rows.team_id,
                                                           rows.player_id_j, strict=True)]
    rows["j_in"] = present
    rows = rows.sort_values(["player_id", "player_id_j", "tip"])
    grp = rows.groupby(["player_id", "player_id_j", "season"], sort=False)
    out = rows[["player_id", "player_id_j", "season", "known_at"]].copy()
    for flag, name in ((False, "wo"), (True, "w")):
        m = (rows.j_in == flag).astype(float)
        out[f"{name}_poss"] = (rows.poss_on * m).groupby([rows.player_id, rows.player_id_j,
                                                          rows.season]).cumsum()  # fmt: skip
        for s in D1_STATS:
            out[f"{name}_{s}"] = (rows[s] * m).groupby([rows.player_id, rows.player_id_j,
                                                        rows.season]).cumsum()  # fmt: skip
    del grp
    return out.sort_values("known_at").reset_index(drop=True)


def d1_factors(c: pd.DataFrame, sums: pd.DataFrame, pads: dict) -> pd.DataFrame:
    """Per candidate: multipliers on D1_STATS from teammates likely to sit (p_play < 0.5)."""
    out = pd.DataFrame(1.0, index=c.index, columns=list(D1_STATS))
    q = c[["game_id", "team_id", "point", "player_id", "p_play", "t", "season"]]
    sitting = q[q.p_play < 0.5][["game_id", "team_id", "point", "player_id", "p_play"]]
    if sitting.empty or sums.empty:
        return out
    pairs = q.reset_index(names="_row").merge(
        sitting.rename(columns={"player_id": "player_id_j", "p_play": "p_j"}),
        on=["game_id", "team_id", "point"])  # fmt: skip
    pairs = pairs[pairs.player_id != pairs.player_id_j]
    if pairs.empty:
        return out
    pairs["player_id"] = pairs.player_id.astype(str)
    pairs["player_id_j"] = pairs.player_id_j.astype(str)
    s = sums.assign(player_id=sums.player_id.astype(str), player_id_j=sums.player_id_j.astype(str))
    m = pd.merge_asof(pairs.sort_values("t"), s, left_on="t", right_on="known_at",
                      by=["player_id", "player_id_j"], suffixes=("", "_s"))  # fmt: skip
    m = m[m.season_s == m.season]
    for st in D1_STATS:
        k = pads[f"player:{st}"]["padding"] if st in RATES else 100.0
        base = (m[f"w_{st}"] + m[f"wo_{st}"]) / (m.w_poss + m.wo_poss).clip(lower=1)
        wo = (m[f"wo_{st}"] + k * base) / (m.wo_poss + k)
        w = (m[f"w_{st}"] + k * base) / (m.w_poss + k)
        ratio = np.where(w > 0, wo / w, 1.0)
        f = 1 + (1 - m.p_j.to_numpy()) * (np.clip(ratio, 0.5, 2.0) - 1)
        agg = pd.Series(f, index=m._row.to_numpy()).groupby(level=0).prod()
        out.loc[agg.index, st] = agg.to_numpy()
    return out


def apply_d1(c: pd.DataFrame, factors: pd.DataFrame) -> pd.DataFrame:
    c = c.copy()
    for st in D1_STATS:
        c[st] = c[st] * factors[st]
    return c


# --- D2 -----------------------------------------------------------------------------------------

def fit_d2(d: pd.DataFrame, act_dreb: np.ndarray, opp_share3: np.ndarray,
           lg_share3: float) -> dict:  # fmt: skip
    """Per role: beta in dreb / expected - 1 = beta (opp three-miss share - league), OLS on
    training player-games that were played."""
    out = {}
    x = opp_share3 - lg_share3
    for role in ("G", "F", "C"):
        sel = (d.role.to_numpy() == role) & np.isfinite(act_dreb) & (d.dreb_m.to_numpy() > 0.5)
        if sel.sum() < 100:
            out[role] = 0.0
            continue
        y = act_dreb[sel] / d.dreb_m.to_numpy()[sel] - 1
        xx = x[sel]
        out[role] = float((xx * (y - y.mean())).sum() / ((xx - xx.mean()) ** 2).sum())
    return out


def roles(c: pd.DataFrame) -> np.ndarray:
    ast36, reb36, tpa36 = 36 * c.ast * 2.08, 36 * (c.oreb + c.dreb) * 2.08, 36 * c.fg3a * 2.08
    return np.where(ast36 >= 5.0, "G", np.where((reb36 >= 9.0) & (tpa36 < 3.0), "C", "F"))


# --- D3 -----------------------------------------------------------------------------------------

def d3_factor(d: pd.DataFrame, gamma: float = 1.0) -> np.ndarray:
    """(teammates' expected eFG% on the floor / league eFG%)^gamma per player."""
    efg = ((d.fg2_pct * d.fg2a + 1.5 * d.fg3_pct * d.fg3a)
           / (d.fg2a + d.fg3a).clip(lower=1e-9))  # fmt: skip
    w = d.p_play * d.minutes * (d.fg2a + d.fg3a)
    k = ["game_id", "point", "team_id"]
    tot = (w * efg).groupby([d[x] for x in k]).transform("sum")
    tw = w.groupby([d[x] for x in k]).transform("sum")
    mates = (tot - w * efg) / (tw - w).clip(lower=1e-9)
    lg = float((w * efg).sum() / w.sum())
    return np.clip((mates / lg).to_numpy(), 0.8, 1.2) ** gamma
