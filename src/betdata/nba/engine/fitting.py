"""M20: fit the engine's constants on training seasons and predict means for any games, as of
each prediction point. Two passes: the first with no non-competitive time gives the engine's
own margin; the second uses E[non-competitive minutes | that margin] (B6 "tonight's script")."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from betdata.nba.engine import project as P
from betdata.nba.engine import team as T
from betdata.nba.engine.states import Params, player_states, season_priors
from betdata.nba.props.panel import team_schedule

G_EDGES = (3, 6, 9, 12, 15, 18, 21, 25, 30, 99)
RETURN_GAP_DAYS = 8


@dataclass
class Inputs:
    g: pd.DataFrame
    p: pd.DataFrame
    tg: pd.DataFrame
    inj: pd.DataFrame

    def __post_init__(self):
        self.sched = team_schedule(self.g)


def g_curve(tg: pd.DataFrame, cm: str, seasons) -> list:
    t = tg[tg.season.isin(list(seasons)) & tg.complete & tg.is_home]
    m = (t.pts - t.o_pts).abs()
    nc = t[f"nc_sec_{cm}"] / 60
    out, lo = [], 0
    for e in G_EDGES:
        sel = (m >= lo) & (m < e)
        out.append((float(e), float(nc[sel].mean()) if sel.any() else 0.0))
        lo = e
    return out


def base_fit(inp: Inputs, pads: dict, params: Params, play_table: dict, seasons) -> P.Fit:
    seasons = list(seasons)
    t = inp.tg[inp.tg.season.isin(seasons)]
    h = t[t.is_home]
    home = float(0.5 * ((h.pts / h.poss).mean() / (h.o_pts / h.o_poss).mean() - 1))
    use = float(((t.fga + 0.44 * t.fta + t.tov) / t.poss).mean())
    p_ot = float((h.game_min > 48).mean())
    return P.Fit(pads=pads, params=params, play_table=play_table,
                 g_curve=g_curve(inp.tg, params.cm, seasons), home=home, use_team=use,
                 p_ot=p_ot, seasons=seasons)  # fmt: skip


def context(inp: Inputs, c: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(second night of a back-to-back, within 2 games of a return from an 8+ day absence)."""
    sc = inp.sched.set_index(["team_id", "game_id"])
    key = pd.MultiIndex.from_frame(c[["team_id", "game_id"]])
    b2b = sc.b2b.reindex(key).to_numpy() > 0
    ret = np.zeros(len(c), bool)
    if "n_since_return" in c:
        ret = c.n_since_return.to_numpy() <= 2
    return b2b, ret


class Engine:
    """States built once per (params); predictions for any games and points."""

    def __init__(self, inp: Inputs, fit: P.Fit, states: pd.DataFrame | None = None):
        self.inp, self.fit = inp, fit
        self.states = states if states is not None else player_states(inp.p, fit.pads,
                                                                      fit.params)  # fmt: skip
        self.priors = season_priors(self.states, fit.pads, fit.params)
        self.tstates = T.team_states(inp.tg, fit.pads)
        self.lstates = T.league_states(inp.tg)
        self._returns()

    def _returns(self):
        """Games since a return from an 8+ day in-season absence, per state row."""
        s = self.states.sort_values(["player_id", "tip"])
        by = s.groupby("player_id", sort=False)
        gap = (s.tip - by.tip.shift(1)).dt.total_seconds() / 86400
        ret = (gap >= RETURN_GAP_DAYS) & (by.season.shift(1) == s.season)
        rid = ret.groupby(s.player_id).cumsum()
        n = s.groupby([s.player_id, rid]).cumcount() + 1
        self.states.loc[s.index, "since_return"] = np.where(rid > 0, n, 99)

    def frame(self, game_ids, points=("pretip",), slots: pd.DataFrame | None = None):
        inp = self.inp
        sl = slots if slots is not None else P.slots_for(inp.g, game_ids, points)
        cand = P.rotation(inp.p, inp.sched, inp.inj, sl)
        c = P.player_frame(self.fit, self.states, self.priors, inp.p, inp.sched, inp.inj, cand)
        st = P.asof_states(self.states[["player_id", "known_at", "since_return", "tip",
                                        "player_name", "season"]].assign(
                                            player_name=""), c)  # fmt: skip
        same = st.season.to_numpy() == c.season.to_numpy()
        # the game after a return: the streak of missed games says so too
        sr = np.where(same, st.since_return.to_numpy(), 99)
        c["n_since_return"] = np.where(c.streak.to_numpy() >= 3, 0, sr)
        return c

    def means(self, c: pd.DataFrame, script: bool = True) -> pd.DataFrame:
        tf = P.team_factors(self.fit, self.tstates, c, self.lstates)
        b2b, ret = context(self.inp, c)
        d = P.means(self.fit, c, tf, None, b2b, ret)
        if not script:
            return d
        gm = P.game_means(d)
        mu = (gm.home_pts - gm.away_pts)
        key = pd.MultiIndex.from_arrays([c.game_id, c.point])
        G = P.expected_g(self.fit.g_curve, mu.reindex(key).to_numpy())
        return P.means(self.fit, c, tf, G, b2b, ret)

    def predict(self, game_ids, points=("pretip",), script: bool = True) -> pd.DataFrame:
        return self.means(self.frame(game_ids, points), script)


def fit_multipliers(eng: Engine, seasons) -> None:
    """Back-to-back and return multipliers on minutes (training residual ratios)."""
    inp = eng.inp
    gids = inp.g[inp.g.season.isin(list(seasons)) & (inp.g.status == "final")].game_id
    d = eng.predict(gids)
    act = actuals(inp.p, d)
    ok = act.notna()
    b2b, _ = context(inp, d)
    for name, sel in (("starter", b2b & d.starter_now.to_numpy()),
                      ("bench", b2b & ~d.starter_now.to_numpy())):
        s = sel & ok.to_numpy()
        eng.fit.b2b[name] = float(act[s].sum() / d.minutes[s].sum()) if s.any() else 1.0
    r = (d.n_since_return.to_numpy() <= 2) & ok.to_numpy()
    eng.fit.ret = float(act[r].sum() / d.minutes[r].sum()) if r.any() else 1.0


def fit_play_by_minutes(eng: Engine, seasons) -> None:
    """B6: P(plays) of unlisted players who played their team's last game, by minutes tier,
    from training targets (the M14 table stays for every listed status)."""
    inp = eng.inp
    eng.fit.play_by_minutes = []
    gids = inp.g[inp.g.season.isin(list(seasons)) & (inp.g.status == "final")].game_id
    c = eng.frame(gids)
    sel = c.status.isna().to_numpy() & (c.streak.to_numpy() == 0)
    played = actuals(inp.p, c).notna().to_numpy()
    lo, out = 0.0, []
    for e in P.MIN_EDGES:
        b = sel & (np.nan_to_num(c.min_l10.to_numpy()) >= lo) & (np.nan_to_num(
            c.min_l10.to_numpy()) < e)  # fmt: skip
        out.append((e, float(played[b].mean()) if b.any() else 0.9))
        lo = e
    eng.fit.play_by_minutes = out


def actuals(p: pd.DataFrame, d: pd.DataFrame, col: str = "minutes") -> pd.Series:
    """The actual `col` of each predicted player-game (NaN: did not play)."""
    a = p[p.played].set_index(["game_id", "player_id"])[col]
    a = a[~a.index.duplicated()]
    return pd.Series(a.reindex(pd.MultiIndex.from_arrays([d.game_id, d.player_id])).to_numpy(),
                     index=d.index)  # fmt: skip


def fit_sim(eng: Engine, seasons, n_games: int = 300, n_sims: int = 2000, seed: int = 7,
            log=print) -> dict:  # fmt: skip
    """B8 constants on a sample of training games: the scoring level (mean simulated total =
    actual), the overtime band (simulated overtime rate = actual), the shooting shock and the
    field-goal dispersion (simulated margin and total spread vs the residual spread of the
    means), and the share dispersions of rebounds and assists (player variance)."""
    from betdata.nba.engine import sim as SM

    inp = eng.inp
    rng = np.random.default_rng(seed)
    pool = inp.g[inp.g.season.isin(list(seasons)) & (inp.g.status == "final")].game_id.to_numpy()
    gids = rng.choice(pool, size=min(n_games, len(pool)), replace=False)
    d = eng.predict(gids)
    tt = inp.tg[inp.tg.is_home].set_index("game_id")
    groups = [(gid, rows) for gid, rows in d.groupby("game_id") if gid in tt.index]
    act_m = np.array([tt.pts[g] - tt.o_pts[g] for g, _ in groups])
    act_t = np.array([tt.pts[g] + tt.o_pts[g] for g, _ in groups])
    sp = dict(SM.DEFAULT_SIM)

    def run(spx):
        eng.fit.sim = spx
        return [SM.sim_game(rows, eng.fit, n_sims, seed=i) for i, (_, rows) in enumerate(groups)]

    # level: one pass, then scale the shooting percentages
    sims = run(sp)
    sim_t = np.array([s.total.mean() for s in sims])
    sp["level"] = float(np.clip(1 + (act_t.mean() / sim_t.mean() - 1) * 1.15, 0.9, 1.1))
    p_ot = eng.fit.p_ot
    best = None
    for shoot in (0.0, 0.02, 0.04):
        for disp in (0.1, 0.3, 0.5):
            spx = {**sp, "sd_shoot": shoot, "fga_disp": disp}
            sims = run(spx)
            mm = np.array([s.margin.mean() for s in sims])
            tm = np.array([s.total.mean() for s in sims])
            gap = (abs(np.mean([s.margin.std() for s in sims]) - (act_m - mm).std())
                   + abs(np.mean([s.total.std() for s in sims]) - (act_t - tm).std()))
            log(f"  sim shoot {shoot} disp {disp}: margin sd "
                f"{np.mean([s.margin.std() for s in sims]):.2f} vs {(act_m - mm).std():.2f}, "
                f"total sd {np.mean([s.total.std() for s in sims]):.2f} vs "
                f"{(act_t - tm).std():.2f}, total bias {(tm - act_t).mean():+.2f}")
            if best is None or gap < best[0]:
                best = (gap, shoot, disp)
    sp["sd_shoot"], sp["fga_disp"] = best[1], best[2]
    # overtime band: P(|script| < band) = the training overtime rate
    sims = run(sp)
    ot_rate = np.mean([(s.ot > 0).mean() for s in sims])
    sp["ot_band"] = float(sp["ot_band"] * p_ot / max(ot_rate, 1e-6))
    eng.fit.sim = sp
    log(f"sim constants: {sp}")
    return sp
