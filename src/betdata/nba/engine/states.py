"""M20 B4-B6: a player's state after each played game, readable by a later prediction once
the game's box score is known (`known_at`).

Within a (player, season) the engine keeps recency-weighted sums S_t = v_t + lambda S_{t-1},
lambda = 0.5^(1/h), for every rate's numerator and denominator and for the minutes shares,
for each half-life h of the grid. An estimate is (S_x + k prior) / (S_n + k), k the B2 padding;
the prior is the player's previous-season estimate regressed to the league by the
season-to-season r (league mean for a player without one). Outlier games enter the minutes
shares with their B4 weights; their rates keep full weight.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.signal import lfilter

from betdata.nba.engine.data import PCTS, RATES, flags

H_GRID = (3, 5, 10, 20, 40)
FLAG_ORDER = ("blowout", "ot", "fouls", "exit")
NEW_TEAM_GAMES = 15
KEY_MIN = 28.0
KEY_WINDOW = 15
KEY_MISSED = 3
ROOKIE_SHARE = 0.30  # comp share prior of a player with no previous season (training mean ~)


def ew(values: np.ndarray, starts: np.ndarray, h: float) -> np.ndarray:
    """S_t = v_t + lambda S_{t-1} restarted at every group start (rows grouped and ordered)."""
    lam = 0.5 ** (1.0 / h)
    v = np.nan_to_num(np.asarray(values, dtype=float))
    s = lfilter([1.0], [1.0, -lam], v)
    idx = np.arange(len(v))
    first = np.maximum.accumulate(np.where(starts, idx, 0))
    carry = np.where(first > 0, s[np.maximum(first - 1, 0)], 0.0)
    return s - carry * lam ** (idx - first + 1)


@dataclass
class Params:
    """Every choice B3-B6 makes on validation (defaults: no down-weighting, one half-life)."""

    cm: str = "cm1"
    w: dict = field(default_factory=lambda: {f: 1.0 for f in FLAG_ORDER})
    h_min_stable: float = 10
    h_min_change: float = 10
    h_rate_stable: float = 20
    h_rate_change: float = 20


def player_states(p: pd.DataFrame, pads: dict, prm: Params,
                  hs: tuple = H_GRID) -> pd.DataFrame:  # fmt: skip
    """One row per played game. Columns per h: `S{h}_<num>` sums; plus role and team facts."""
    d = p[p.played].sort_values(["player_id", "tip"]).reset_index()
    d = d.rename(columns={"index": "_prow"})
    f = flags(p, prm.cm).loc[d._prow].reset_index(drop=True)
    wt = np.ones(len(d))
    for k in FLAG_ORDER:
        wt = np.where(f[k].to_numpy(), wt * prm.w.get(k, 1.0), wt)
    d["w_min"] = wt
    d["c"] = d[f"c_{prm.cm}"]
    d["g"] = d[f"g_{prm.cm}"]
    d["use"] = d.fga + 0.44 * d.fta + d.tov
    grp = d.player_id.astype(str) + "|" + d.season.astype(str)
    starts = (grp != grp.shift(1)).to_numpy()
    out = d[["player_id", "player_name", "team_id", "game_id", "season", "tip", "known_at",
             "starter"]].copy()  # fmt: skip
    nums = {"poss": d.poss_on, **{r: d[r] for r in RATES}, "use": d.use}
    for k, (m, a) in PCTS.items():
        nums[f"{k}m"], nums[f"{k}a"] = d[m], d[a]
    nums["w"] = d.w_min
    nums["wc"] = d.w_min * d.c
    gok = d.g.notna()
    nums["gw"] = np.where(gok, d.w_min, 0.0)
    nums["gwg"] = np.where(gok, d.w_min * d.g.fillna(0), 0.0)
    for h in hs:
        for k, v in nums.items():
            out[f"S{h}_{k}"] = ew(v.to_numpy() if hasattr(v, "to_numpy") else v, starts, h)
    # role facts (B5)
    by = d.groupby("player_id", sort=False)
    st = d.starter.astype(float)
    out["start_l3"] = by.starter.transform(lambda s: s.astype(float).rolling(3, 1).mean())
    prev10 = by.starter.transform(lambda s: s.astype(float).shift(3).rolling(10, 3).mean())
    out["start_p10"] = prev10
    del st
    stint = (d.team_id != by.team_id.shift(1)).groupby(d.player_id).cumsum()
    out["n_team"] = d.groupby([d.player_id, stint]).cumcount() + 1
    out["had_other_team"] = (stint > 1).to_numpy()
    out["min_l10"] = by.minutes.transform(lambda s: s.rolling(10, 1).mean())
    out["n_season"] = d.groupby(grp.to_numpy()).cumcount() + 1
    return out


def season_priors(states: pd.DataFrame, pads: dict, prm: Params) -> pd.DataFrame:
    """Per (player, season): the prior for the season, from his previous season's final
    estimates regressed to the league by the season-to-season r."""
    last = states.sort_values("tip").groupby(["player_id", "season"]).tail(1)
    est = estimates(last, pads, prm, prior=None, change=np.zeros(len(last), bool))
    est = est.assign(player_id=last.player_id.to_numpy(), season=last.season.to_numpy() + 1)
    return est.set_index(["player_id", "season"])


def _league(pads: dict, name: str) -> float:
    return pads[name]["league"]


def estimates(st: pd.DataFrame, pads: dict, prm: Params, prior: pd.DataFrame | None,
              change: np.ndarray, league_now: dict | None = None) -> pd.DataFrame:  # fmt: skip
    """Shrunk estimates from state rows `st` (aligned with `prior` rows and `change` flags):
    per-possession rates, percentages, usage and the two minutes shares."""
    out = pd.DataFrame(index=st.index)
    hr = np.where(change, prm.h_rate_change, prm.h_rate_stable)
    hm = np.where(change, prm.h_min_change, prm.h_min_stable)

    def pick(col: str, hv: np.ndarray) -> np.ndarray:
        res = np.full(len(st), np.nan)
        for h in np.unique(hv):
            sel = hv == h
            res[sel] = st[f"S{int(h)}_{col}"].to_numpy()[sel]
        return res

    def pri(name: str, key: str) -> np.ndarray:
        # the league's level as of the point (era), else the training average
        lg = (league_now[key] if league_now is not None and key in league_now
              else _league(pads, key))  # fmt: skip
        r = pads[key]["season_r"]
        if prior is None or name not in prior:
            return np.full(len(st), lg)
        x = prior[name].to_numpy(dtype=float)
        return np.where(np.isfinite(x), r * x + (1 - r) * lg, lg)

    n = pick("poss", hr)
    for r_ in (*RATES, "use"):
        key = f"player:{'usage' if r_ == 'use' else r_}"
        k = pads[key]["padding"]
        out[r_] = (pick(r_, hr) + k * pri(r_, key)) / (n + k)
    for k_, _ in PCTS.items():
        key = f"player:{k_}_pct"
        pad = pads[key]["padding"]
        out[f"{k_}_pct"] = (pick(f"{k_}m", hr) + pad * pri(f"{k_}_pct", key)) / (
            pick(f"{k_}a", hr) + pad)
    kc = pads["player:comp_share"]["padding"]
    pc = pri("c", "player:comp_share")
    if prior is not None and "c" in prior:
        pc = np.where(np.isfinite(prior["c"].to_numpy(dtype=float)), pc, ROOKIE_SHARE)
    out["c"] = (pick("wc", hm) + kc * pc) / (pick("w", hm) + kc)
    kg = pads["player:garbage_share"]["padding"]
    out["g"] = (pick("gwg", hm) + kg * pri("g", "player:garbage_share")) / (pick("gw", hm) + kg)
    return out
