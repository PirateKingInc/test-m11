"""M20 B5-B7: as-of targets, minutes and team-consistent means (MODEL.md "M20").

For each game and prediction point both teams' rotations are assembled from what is known at
the point: players who played for the team in its last 10 games (and still belong to it), plus
anyone the official report lists for the game. Each player gets his shrunk rates and minutes
shares (states.py), his play probability (M14 table), his role-change state, and his team's and
opponent's as-of rates (team.py). Means:

  minutes  c (48 - G) + g G, the shares rescaled so the expected players fill 5 slots (a player
           is projected conditional on playing), x rest/return multipliers, capped at 44;
           G = E[non-competitive minutes | the engine's own margin]
  shots    per-possession rates x on-court possessions x the opponent's allowed factor, the
           team's usage rescaled to its expected total (shots sum to the team's)
  makes    percentages x opponent allowed factor x home court
  rebounds team misses x log5 rebound rates, split by rate x minutes
  assists  team made FG x assisted share, split by rate x minutes
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import norm

from betdata.nba.engine import team as T
from betdata.nba.engine.states import (
    KEY_MIN,
    KEY_MISSED,
    KEY_WINDOW,
    NEW_TEAM_GAMES,
    Params,
    estimates,
)
from betdata.nba.props.panel import injuries_asof, play_probs

ROTATION = 10
CAP = 44.0
MARGIN_SD = 13.0  # spread of the final margin around its mean, for E[G] in the means


@dataclass
class Fit:
    """Constants fitted on training seasons (`fit_constants`)."""

    pads: dict
    params: Params
    play_table: dict
    g_curve: list = field(default_factory=list)  # [(|margin| upper edge, mean nc minutes)]
    home: float = 0.0  # make% multiplier: home x (1 + home), away x (1 - home)
    b2b: dict = field(default_factory=lambda: {"starter": 1.0, "bench": 1.0})
    ret: float = 1.0
    use_team: float = 1.13  # team (FGA + 0.44 FTA + TOV) per possession
    p_ot: float = 0.06
    league: dict = field(default_factory=dict)
    seasons: list = field(default_factory=list)
    sim: dict = field(default_factory=dict)  # B8 dispersions and shocks
    # B6: P(plays) of a player not on the report who played his team's last game, by his
    # minutes over his last 10 (the M14 table gives one number for all of them)
    play_by_minutes: list = field(default_factory=list)  # [(upper edge, prob)]

    def to_json(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_json(cls, d: dict) -> Fit:
        d = dict(d)
        d["params"] = Params(**d["params"])
        return cls(**d)


MIN_EDGES = (8.0, 14.0, 20.0, 26.0, 32.0, 99.0)


def play_by_minutes(table: list, m) -> np.ndarray:
    edges = np.array([e for e, _ in table])
    vals = np.array([v for _, v in table])
    return vals[np.clip(np.searchsorted(edges, np.nan_to_num(np.asarray(m, float))), 0,
                        len(vals) - 1)]  # fmt: skip


def g_of(curve: list, m: np.ndarray) -> np.ndarray:
    """E[non-competitive minutes] given |margin| from the fitted step curve."""
    edges = np.array([e for e, _ in curve])
    vals = np.array([v for _, v in curve])
    i = np.clip(np.searchsorted(edges, np.abs(m)), 0, len(vals) - 1)
    return vals[i]


def expected_g(curve: list, mu: np.ndarray, sd: float = MARGIN_SD) -> np.ndarray:
    """E over margin ~ N(mu, sd) of g_of(|margin|), on a fixed grid."""
    z = np.linspace(-3, 3, 25)
    w = norm.pdf(z)
    w /= w.sum()
    vals = np.stack([g_of(curve, mu + sd * zi) for zi in z])
    return (w[:, None] * vals).sum(0)


# --- targets ----------------------------------------------------------------------------------

def slots_for(g: pd.DataFrame, game_ids, points) -> pd.DataFrame:
    from betdata.nba.props.panel import point_times

    s = point_times(g, list(game_ids), points)
    both = []
    for side in ("home", "away"):
        x = s.copy()
        x["team_id"] = x.game_id.map(g[side])
        x["opp_id"] = x.game_id.map(g["away" if side == "home" else "home"])
        x["is_home"] = side == "home"
        both.append(x)
    out = pd.concat(both, ignore_index=True)
    out["season"] = out.game_id.map(g.season)
    return out


def rotation(p: pd.DataFrame, sched: pd.DataFrame, inj: pd.DataFrame,
             slots: pd.DataFrame) -> pd.DataFrame:  # fmt: skip
    """Candidates per slot: recent players of the team, plus report-listed players."""
    idx = sched.set_index(["team_id", "game_id"]).idx
    sl = slots.copy()
    sl["idx"] = idx.reindex(pd.MultiIndex.from_frame(sl[["team_id", "game_id"]])).to_numpy()
    played = p[p.played][["team_id", "game_id", "player_id"]].copy()
    played["i"] = idx.reindex(pd.MultiIndex.from_frame(played[["team_id", "game_id"]])).to_numpy()
    back = pd.concat([sl.assign(i=sl.idx - k) for k in range(1, ROTATION + 1)])
    cand = back.merge(played[["team_id", "player_id", "i"]], on=["team_id", "i"])
    listed = inj.dropna(subset=["player_id"])[["game_id", "team_id", "player_id", "known_at"]]
    lst = sl.merge(listed, on=["game_id", "team_id"])
    lst = lst[lst.known_at <= lst.t].drop(columns="known_at")
    cols = ["game_id", "team_id", "opp_id", "is_home", "point", "t", "season", "idx",
            "player_id"]  # fmt: skip
    out = pd.concat([cand[cols], lst[cols]]).drop_duplicates(
        ["game_id", "team_id", "point", "player_id"])  # fmt: skip
    return out.reset_index(drop=True)


def asof_states(states: pd.DataFrame, keys: pd.DataFrame) -> pd.DataFrame:
    k = keys[["player_id", "t"]].reset_index(names="_row").sort_values("t")
    k["player_id"] = k.player_id.astype(str)
    s = states.drop(columns=["player_name"])
    if s.player_id.dtype != k.player_id.dtype:
        s = s.assign(player_id=s.player_id.astype(str))
    m = pd.merge_asof(k, s.sort_values("known_at"),
                      left_on="t", right_on="known_at", by="player_id",
                      allow_exact_matches=True)  # fmt: skip
    return m.sort_values("_row").set_index("_row")


def player_frame(fit: Fit, states: pd.DataFrame, priors: pd.DataFrame, p: pd.DataFrame,
                 sched: pd.DataFrame, inj: pd.DataFrame,
                 cand: pd.DataFrame) -> pd.DataFrame:  # fmt: skip
    """Per candidate: as-of state, role change, play probability and shrunk estimates."""
    c = cand.copy()
    st = asof_states(states, c)
    has = st.known_at.notna().to_numpy()
    c = c[has].reset_index(drop=True)
    st = st[has].reset_index(drop=True)
    same_team = st.team_id.to_numpy() == c.team_id.to_numpy()
    tidx = sched.set_index(["team_id", "game_id"]).idx
    st_idx = tidx.reindex(pd.MultiIndex.from_arrays([st.team_id, st.game_id])).to_numpy()
    c["streak"] = np.where(same_team, np.clip(c.idx.to_numpy() - st_idx - 1, 0, 82), 0)
    # who left the team: not in rotation (unless the report lists him for this team)
    c["same_team"] = same_team
    same_season = st.season.to_numpy() == c.season.to_numpy()
    new_team = (~same_team) | ((st.n_team.to_numpy() < NEW_TEAM_GAMES)
                               & st.had_other_team.to_numpy())  # fmt: skip
    sw = (((st.start_l3 >= 2 / 3) & (st.start_p10 < 0.3))
          | ((st.start_l3 <= 1 / 3) & (st.start_p10 >= 0.7))).to_numpy()  # fmt: skip
    c["new_team"] = new_team
    c["starter_switch"] = sw
    c["starter_now"] = (st.start_l3 >= 0.5).to_numpy()
    c["min_l10"] = st.min_l10.to_numpy()
    c["n_season"] = np.where(same_season, st.n_season.to_numpy(), 0)
    # S sums only count within the target's season
    s2 = st.copy()
    scols = [x for x in st.columns if x.startswith("S") and "_" in x]
    s2.loc[~same_season, scols] = 0.0
    pk = pd.MultiIndex.from_arrays([c.player_id, c.season])
    pri = priors.reindex(pk).reset_index(drop=True)
    c["_s2"] = 0  # placeholder to keep index aligned
    ist = injuries_asof(inj, c[["game_id", "team_id", "player_id", "t"]])
    c["status"] = ist.status.to_numpy()
    c["p_play"] = play_probs(c.status, c.streak, ist.reported, fit.play_table)
    if fit.play_by_minutes:
        unlisted = c.status.isna().to_numpy() & (c.streak.to_numpy() == 0)
        c["p_play"] = np.where(unlisted, play_by_minutes(fit.play_by_minutes, c.min_l10),
                               c.p_play)  # fmt: skip
    c = c.drop(columns="_s2")
    c["key_gone"] = False  # filled per slot below
    c = c[(c.same_team) | c.status.notna()].copy()
    keep = c.index.to_numpy()
    c = c.reset_index(drop=True)
    s2 = s2.loc[keep].reset_index(drop=True)
    pri = pri.loc[keep].reset_index(drop=True)
    # key teammate gone (B5): a >= 28-minute teammate of the last 15 games who left or has
    # missed 3+ straight team games
    gone = _key_gone(p, sched, states, c)
    c["key_gone"] = gone
    c["role_change"] = c.new_team | c.starter_switch | c.key_gone
    est = estimates(s2, fit.pads, fit.params, pri, c.role_change.to_numpy())
    for col in est.columns:
        c[col] = est[col].to_numpy()
    return c


def _key_gone(p, sched, states, c) -> np.ndarray:
    idx = sched.set_index(["team_id", "game_id"]).idx
    played = p[p.played][["team_id", "game_id", "player_id"]].copy()
    played["i"] = idx.reindex(pd.MultiIndex.from_frame(played[["team_id", "game_id"]])).to_numpy()
    sl = c[["game_id", "team_id", "point", "t", "idx"]].drop_duplicates(
        ["game_id", "team_id", "point"])  # fmt: skip
    back = pd.concat([sl.assign(i=sl.idx - k) for k in range(1, KEY_WINDOW + 1)])
    cand = back.merge(played[["team_id", "player_id", "i"]], on=["team_id", "i"])
    cand = cand.drop_duplicates(["game_id", "team_id", "point", "player_id"]).reset_index(
        drop=True)  # fmt: skip
    st = asof_states(states, cand)
    st_idx = idx.reindex(pd.MultiIndex.from_arrays([st.team_id, st.game_id])).to_numpy()
    left = st.team_id.to_numpy() != cand.team_id.to_numpy()
    missed = (cand.idx.to_numpy() - st_idx - 1) >= KEY_MISSED
    big = st.min_l10.to_numpy() >= KEY_MIN
    cand["gone"] = big & (left | missed)
    g = cand[cand.gone].groupby(["game_id", "team_id", "point"]).player_id.apply(set)
    key = list(zip(c.game_id, c.team_id, c.point, strict=True))
    return np.array([bool(g.get(k, set()) - {pid}) for k, pid in zip(key, c.player_id,
                                                                     strict=True)])  # fmt: skip


# --- means ------------------------------------------------------------------------------------

def team_factors(fit: Fit, tstates: pd.DataFrame, c: pd.DataFrame,
                 lstates: pd.DataFrame | None = None) -> pd.DataFrame:  # fmt: skip
    """Own and opponent as-of rates; opponent factors relative to the league as of the point."""
    lg = {k: fit.pads[f"team:{k}"]["league"] for k in T.TEAM_RATES}
    own = T.asof(tstates, c.team_id, c.t, c.season).fillna(pd.Series(lg))
    opp = T.asof(tstates, c.opp_id, c.t, c.season).fillna(pd.Series(lg))
    fb = {**lg, "use": fit.use_team, "credit": 0.94}
    now = (T.league_asof(lstates, c.t, fb) if lstates is not None
           else pd.DataFrame({k: np.full(len(c), v) for k, v in fb.items()}))
    out = pd.DataFrame(index=c.index)
    out["pace_own"] = own.pace.to_numpy()
    out["pace_opp"] = opp.pace.to_numpy()
    out["f_fg3a"] = (opp.fg3a_allowed / now.fg3a_allowed).to_numpy()
    out["f_fg2p"] = (opp.fg2_pct_allowed / now.fg2_pct_allowed).to_numpy()
    out["f_fg3p"] = (opp.fg3_pct_allowed / now.fg3_pct_allowed).to_numpy()
    out["f_tov"] = (opp.tov_forced / now.tov_forced).to_numpy()
    out["oreb_rate"] = own.oreb_rate.to_numpy()
    out["dreb_rate"] = own.dreb_rate.to_numpy()
    out["o_oreb_rate"] = opp.oreb_rate.to_numpy()
    out["o_dreb_rate"] = opp.dreb_rate.to_numpy()
    out["ast_share"] = own.ast_share.to_numpy()
    out["league_pace"] = now.pace.to_numpy()
    out["use_team"] = now.use.to_numpy()
    out["lg_oreb"] = now.oreb_rate.to_numpy()
    out["credit"] = now.credit.to_numpy()
    return out


def log5(a: np.ndarray, b: np.ndarray, lg: float) -> np.ndarray:
    """P(offence rebounds a miss): offence rate a vs the defence's offensive-rebound-allowed
    rate 1 - b, against the league's rate lg."""
    x = np.asarray(a)
    y = 1 - np.asarray(b)
    num = x * y / lg
    return num / (num + (1 - x) * (1 - y) / (1 - lg))


def means(fit: Fit, c: pd.DataFrame, tf: pd.DataFrame, gmin: np.ndarray | None = None,
          b2b: np.ndarray | None = None,
          ret: np.ndarray | None = None) -> pd.DataFrame:  # fmt: skip
    """Expected minutes and stats for each candidate, conditional on him playing."""
    d = pd.concat([c, tf], axis=1)
    k = ["game_id", "point", "team_id"]
    lg_g = np.zeros(len(d)) if gmin is None else gmin
    d["G"] = lg_g
    base = d.c * (48 - d.G) + np.nan_to_num(d.g) * d.G
    mult = np.ones(len(d))
    if b2b is not None:
        mult = np.where(b2b, np.where(d.starter_now, fit.b2b["starter"], fit.b2b["bench"]), mult)
    if ret is not None:
        mult = np.where(ret, mult * fit.ret, mult)
    d["m_raw"] = base * mult
    d["pm"] = d.p_play * d.m_raw
    tot = d.groupby(k).pm.transform("sum")
    reg = 240.0
    d["minutes"] = np.minimum(CAP, d.m_raw * reg / (tot - d.pm + d.m_raw).clip(lower=1e-6))
    ot = 1 + fit.p_ot * 5 / 48
    d["minutes"] *= ot
    poss = d.pace_own * d.pace_opp / d.league_pace * ot
    d["poss_on"] = poss * d.minutes / (48 * ot)
    # usage: the team's expected shot volume per possession is its usual one
    use = d.fg2a + d.fg3a * d.f_fg3a + 0.44 * d.fta + d.tov * d.f_tov
    d["_u"] = d.p_play * d.minutes / (48 * ot) * use
    tot_u = d.groupby(k)._u.transform("sum")
    me_u = d.minutes / (48 * ot) * use
    scale = d.use_team / (tot_u - d._u + me_u).clip(lower=1e-6)
    d["fg2a_m"] = d.fg2a * scale * d.poss_on
    d["fg3a_m"] = d.fg3a * d.f_fg3a * scale * d.poss_on
    d["fta_m"] = d.fta * scale * d.poss_on
    d["tov_m"] = d.tov * d.f_tov * scale * d.poss_on
    home = np.where(d.is_home, 1 + fit.home, 1 - fit.home)
    d["fg2m_m"] = d.fg2a_m * np.clip(d.fg2_pct * d.f_fg2p * home, 0, 1)
    d["fg3m_m"] = d.fg3a_m * np.clip(d.fg3_pct * d.f_fg3p * home, 0, 1)
    d["ftm_m"] = d.fta_m * np.clip(d.ft_pct, 0, 1)
    d["pts_m"] = 2 * d.fg2m_m + 3 * d.fg3m_m + d.ftm_m
    # team totals (expected players), for rebounds and assists
    w = d.p_play
    for col in ("fg2a_m", "fg3a_m", "fg2m_m", "fg3m_m", "pts_m"):
        d[f"T_{col}"] = (w * d[col]).groupby([d[x] for x in k]).transform("sum")
    d["T_miss"] = d.T_fg2a_m + d.T_fg3a_m - d.T_fg2m_m - d.T_fg3m_m
    opp_key = ["game_id", "point", "team_id"]
    tm = d.groupby(opp_key).agg(miss=("T_miss", "first"), pts=("T_pts_m", "first"),
                                fgm=("T_fg2m_m", "first"))  # fmt: skip
    tm["fgm"] = tm.fgm + d.groupby(opp_key).T_fg3m_m.first()
    ok = pd.MultiIndex.from_arrays([d.game_id, d.point, d.opp_id])
    d["O_miss"] = tm["miss"].reindex(ok).to_numpy()
    d["O_pts"] = tm["pts"].reindex(ok).to_numpy()
    p_or = log5(d.oreb_rate, d.o_dreb_rate, d.lg_oreb)  # we rebound our misses
    p_dr = 1 - log5(d.o_oreb_rate, d.dreb_rate, d.lg_oreb)  # we rebound their misses
    for stat, team_total in (("oreb", d.T_miss * d.credit * p_or),
                             ("dreb", d.O_miss * d.credit * p_dr)):
        wv = d[stat] * d.minutes
        tw = (w * wv).groupby([d[x] for x in k]).transform("sum")
        d[f"{stat}_m"] = team_total * wv / (tw - w * wv + wv).clip(lower=1e-9)
    t_fgm = tm["fgm"].reindex(pd.MultiIndex.from_arrays([d.game_id, d.point, d.team_id]))
    wa = d.ast * d.minutes
    twa = (w * wa).groupby([d[x] for x in k]).transform("sum")
    d["ast_m"] = t_fgm.to_numpy() * d.ast_share * wa / (twa - w * wa + wa).clip(lower=1e-9)
    d["reb_m"] = d.oreb_m + d.dreb_m
    d["pra_m"] = d.pts_m + d.reb_m + d.ast_m
    return d


def game_means(d: pd.DataFrame) -> pd.DataFrame:
    """Per (game, point): expected home and away points from the players."""
    t = d.groupby(["game_id", "point", "is_home"]).T_pts_m.first().unstack("is_home")
    return pd.DataFrame({"home_pts": t[True], "away_pts": t[False]})
