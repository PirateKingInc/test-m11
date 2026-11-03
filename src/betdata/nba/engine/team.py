"""M20 B7 inputs: each team's as-of rates after every game (pace, efficiency, rebounding, the
shooting and turnovers it allows, its assisted share), recency-weighted within a season
(half-life H_TEAM games), shrunk with the B2 padding to the previous season regressed by the
season-to-season r. Read for a later game once the game's box score is known."""

from __future__ import annotations

import numpy as np
import pandas as pd

from betdata.nba.engine.states import ew

H_TEAM = 20  # games; fixed (MODEL.md "M20 B7")
# rate -> (numerator column, denominator column) on team-games
TEAM_RATES = {
    "pace": ("_pace48", "_one"),
    "off_ppp": ("pts", "poss"),
    "def_ppp": ("o_pts", "o_poss"),
    "oreb_rate": ("oreb", "_oreb_ch"),
    "dreb_rate": ("dreb", "_dreb_ch"),
    "fg3a_allowed": ("o_fg3a", "o_poss"),
    "fg2_pct_allowed": ("o_fg2m", "o_fg2a"),
    "fg3_pct_allowed": ("o_fg3m", "o_fg3a"),
    "tov_forced": ("o_tov", "o_poss"),
    "ast_share": ("ast", "fgm"),
}


def prepare(tg: pd.DataFrame) -> pd.DataFrame:
    t = tg.copy()
    t["_pace48"] = 48 * t.poss / t.game_min
    t["_one"] = 1.0
    t["_oreb_ch"] = t.oreb + t.o_dreb
    t["_dreb_ch"] = t.dreb + t.o_oreb
    t["o_fg2m"] = t.o_fgm - t.o_fg3m
    t["o_fg2a"] = t.o_fga - t.o_fg3a
    return t


def team_states(tg: pd.DataFrame, pads: dict) -> pd.DataFrame:
    """After each team-game: the shrunk estimate of every TEAM_RATES rate."""
    t = prepare(tg).dropna(subset=["o_poss"]).sort_values(["team_id", "tip"]).reset_index(
        drop=True)  # fmt: skip
    grp = t.team_id.astype(str) + "|" + t.season.astype(str)
    starts = (grp != grp.shift(1)).to_numpy()
    out = t[["team_id", "game_id", "season", "tip", "known_at"]].copy()
    sums = {}
    for name, (num, den) in TEAM_RATES.items():
        sums[name] = (ew(t[num].to_numpy(), starts, H_TEAM), ew(t[den].to_numpy(), starts,
                                                               H_TEAM))  # fmt: skip
    # season-end estimates -> next season's prior
    raw = {n: (sx + pads[f"team:{n}"]["padding"] * pads[f"team:{n}"]["league"])
           / (sn + pads[f"team:{n}"]["padding"]) for n, (sx, sn) in sums.items()}
    last = pd.DataFrame({"team_id": t.team_id, "season": t.season + 1, **raw}).groupby(
        ["team_id", "season"]).tail(1).set_index(["team_id", "season"])  # fmt: skip
    pk = pd.MultiIndex.from_arrays([t.team_id, t.season])
    for name, (sx, sn) in sums.items():
        pd_ = pads[f"team:{name}"]
        prev = last[name].reindex(pk).to_numpy()
        prior = np.where(np.isfinite(prev), pd_["season_r"] * prev
                         + (1 - pd_["season_r"]) * pd_["league"], pd_["league"])  # fmt: skip
        out[name] = (sx + pd_["padding"] * prior) / (sn + pd_["padding"])
    out["season_prior"] = False
    # a season's first game reads the season prior only: one extra row per (team, season)
    first = t.groupby(["team_id", "season"]).head(1)
    pri = first[["team_id", "season", "tip"]].copy()
    pri["game_id"] = -1
    pri["known_at"] = pri.tip - pd.Timedelta(days=60)
    for name in TEAM_RATES:
        pd_ = pads[f"team:{name}"]
        prev = last[name].reindex(pd.MultiIndex.from_arrays([pri.team_id, pri.season])).to_numpy()
        pri[name] = np.where(np.isfinite(prev), pd_["season_r"] * prev
                             + (1 - pd_["season_r"]) * pd_["league"], pd_["league"])  # fmt: skip
    pri["season_prior"] = True
    return pd.concat([out, pri], ignore_index=True).sort_values("known_at").reset_index(
        drop=True)  # fmt: skip


def asof(states: pd.DataFrame, team_ids, ts, seasons) -> pd.DataFrame:
    """Each (team, t): the latest state known by t of the same season (else the league)."""
    k = pd.DataFrame({"team_id": np.asarray(team_ids), "t": pd.to_datetime(ts, utc=True),
                      "season": np.asarray(seasons)}).reset_index(names="_row")  # fmt: skip
    m = pd.merge_asof(k.sort_values("t"), states.drop(columns=["tip", "game_id"]),
                      left_on="t", right_on="known_at", by="team_id",
                      suffixes=("", "_s")).sort_values("_row")  # fmt: skip
    same = m.season_s.to_numpy() == m.season.to_numpy()
    out = m[list(TEAM_RATES)].reset_index(drop=True)
    out.loc[~same, :] = np.nan
    return out


H_LEAGUE = 300  # team-games (about three weeks of the league)
# credit: the share of a team's missed field goals rebounded by a player (the rest are team
# rebounds, which box scores do not give any player)
LEAGUE_RATES = {**TEAM_RATES, "use": ("_use", "poss"), "credit": ("_reb_ch", "_miss")}


def league_states(tg: pd.DataFrame) -> pd.DataFrame:
    """The league's current level of every rate: recency-weighted over all team-games in
    `known_at` order (half-life H_LEAGUE team-games), so opponent factors are relative to
    the era, not the training average."""
    t = prepare(tg).dropna(subset=["o_poss"]).sort_values(
        ["known_at", "game_id", "team_id"]).reset_index(drop=True)  # fmt: skip
    t["_use"] = t.fga + 0.44 * t.fta + t.tov
    t["_reb_ch"] = t.oreb + t.o_dreb
    t["_miss"] = t.fga - t.fgm
    starts = np.zeros(len(t), bool)
    if len(t):
        starts[0] = True
    out = t[["known_at"]].copy()
    for name, (num, den) in LEAGUE_RATES.items():
        out[name] = ew(t[num].to_numpy(), starts, H_LEAGUE) / ew(t[den].to_numpy(), starts,
                                                                 H_LEAGUE)  # fmt: skip
    return out.groupby("known_at").tail(1).reset_index(drop=True)


def league_asof(ls: pd.DataFrame, ts, fallback: dict) -> pd.DataFrame:
    k = pd.DataFrame({"t": pd.to_datetime(ts, utc=True)}).reset_index(names="_row")
    m = pd.merge_asof(k.sort_values("t"), ls, left_on="t", right_on="known_at").sort_values(
        "_row")  # fmt: skip
    out = m[list(LEAGUE_RATES)].reset_index(drop=True)
    return out.fillna(pd.Series(fallback))
