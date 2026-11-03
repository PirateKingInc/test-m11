"""M20 B7-B8: simulate a game 10,000 times from its players (MODEL.md "M20 B7", "B8").

Game script ties the players together. Each simulation draws, in stage 1, who plays, the
game's possessions, a shooting shock per team and a script margin (the means plus the shocks
plus count noise); the script margin sets the non-competitive minutes and overtime. Stage 2
draws minutes (filled to 240 + 25 per overtime), shots, makes, rebounds from the misses and
assists from the made shots. Team totals are sums of the players' by construction.

`sim_game` takes one game's rows of `project.means` (both teams, one point) and returns a
`GameSim` with per-simulation team points and per-player stat draws.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from betdata.nba.engine.project import CAP, g_of

N_SIMS = 10_000
DEFAULT_SIM = {"sd_pace": 3.0, "sd_shoot": 0.045, "sd_script": 10.0, "fga_disp": 0.5,
               "k_shot": 6.0, "level": 1.0,
               "k_reb": 8.0, "k_ast": 6.0, "ot_band": 1.5}  # fmt: skip
STATS = ("pts", "reb", "ast", "fg3m", "pra", "minutes")


@dataclass
class GameSim:
    game_id: int
    home_pts: np.ndarray  # (S,)
    away_pts: np.ndarray
    ot: np.ndarray  # (S,) overtime periods
    players: pd.DataFrame  # one row per player: player_id, team_id, is_home, ...
    draws: dict  # stat -> (S, n) array (NaN where the player did not play)
    checks: dict

    @property
    def margin(self) -> np.ndarray:
        return self.home_pts - self.away_pts

    @property
    def total(self) -> np.ndarray:
        return self.home_pts + self.away_pts

    def p_home_win(self) -> float:
        m = self.margin
        return float((m > 0).mean() + 0.5 * (m == 0).mean())

    def p_cover(self, home_line: float) -> tuple[float, float]:
        """(P(home covers), P(push)) at the home spread `home_line` (e.g. -4.5)."""
        x = self.margin + home_line
        return float((x > 0).mean()), float((x == 0).mean())

    def p_over(self, line: float) -> tuple[float, float]:
        t = self.total
        return float((t > line).mean()), float((t == line).mean())

    def player_p_over(self, j: int, stat: str, line: float) -> tuple[float, float] | None:
        """(P(over), P(push)) given the player plays; None if he never plays."""
        x = self.draws[stat][:, j]
        x = x[~np.isnan(x)]
        if len(x) == 0:
            return None
        return float((x > line).mean()), float((x == line).mean())

    def player_dist(self, j: int, stat: str) -> dict | None:
        x = self.draws[stat][:, j]
        x = x[~np.isnan(x)]
        if len(x) == 0:
            return None
        q = np.quantile(x, [0.1, 0.25, 0.5, 0.75, 0.9])
        return {"mean": float(x.mean()), "median": float(q[2]), "q10": float(q[0]),
                "q25": float(q[1]), "q75": float(q[3]), "q90": float(q[4]),
                "p_play": float(len(x) / len(self.draws[stat]))}


def _nb(rng, mean: np.ndarray, r: float) -> np.ndarray:
    """Negative binomial counts with the given means and dispersion r (gamma-Poisson);
    r = 0 means Poisson."""
    if not r:
        return rng.poisson(np.maximum(mean, 1e-9))
    lam = rng.gamma(r, np.maximum(mean, 1e-9) / r)
    return rng.poisson(lam)


def _split(rng, total: np.ndarray, weights: np.ndarray, k: float) -> np.ndarray:
    """Split each simulation's team total among players: multinomial with gamma-perturbed
    weights (shape k; larger k = steadier shares). weights (S, n), zero for non-players."""
    w = np.where(weights > 0, rng.gamma(k, np.maximum(weights, 1e-12) / k), 0.0)
    s = w.sum(1, keepdims=True)
    pv = np.where(s > 0, w / np.where(s > 0, s, 1), 1.0 / w.shape[1])
    return rng.multinomial(total.astype(np.int64), pv)


MIN_PLAYERS = 8  # an NBA team dresses at least eight


def _min_players(plays: np.ndarray, p: np.ndarray, home: np.ndarray) -> np.ndarray:
    """A team with fewer than MIN_PLAYERS drawn to play gets its likeliest others added."""
    out = plays.copy()
    for side in (True, False):
        idx = np.where(home == side)[0]
        if len(idx) == 0:
            continue
        order = idx[np.argsort(-p[idx], kind="stable")]
        need = min(MIN_PLAYERS, len(idx))
        for _ in range(need):
            short = out[:, idx].sum(1) < need
            if not short.any():
                break
            # add the likeliest player not yet playing
            for j in order:
                add = short & ~out[:, j]
                out[add, j] = True
                short = out[:, idx].sum(1) < need
                if not short.any():
                    break
    return out


def _minutes(c, g, mult, plays, G, ot):
    """(S, n) minutes: c (48 - G) + g G for the players who play, filled to 240 + 25 OT, each
    capped (CAP, +5 per overtime period), the excess spread over the others."""
    base = (c[None, :] * (48 - G[:, None]) + g[None, :] * G[:, None]) * mult[None, :]
    base = np.where(plays, np.maximum(base, 0.5), 0.0)
    target = (240.0 + 25.0 * ot)[:, None]
    cap = (CAP + 5.0 * ot)[:, None]
    m = base * target / np.maximum(base.sum(1, keepdims=True), 1e-9)
    for _ in range(8):
        over = np.maximum(m - cap, 0)
        if not over.any():
            break
        m = np.minimum(m, cap)
        free = np.where(plays & (m < cap), m, 0)
        m = m + over.sum(1, keepdims=True) * free / np.maximum(free.sum(1, keepdims=True), 1e-9)
    return m


def sim_game(rows: pd.DataFrame, fit, n: int = N_SIMS, seed: int = 0) -> GameSim:
    """Simulate one game from its players' rows (both teams, one point)."""
    sp = {**DEFAULT_SIM, **(fit.sim or {})}
    rng = np.random.default_rng(seed)
    r = rows.reset_index(drop=True)
    home = r.is_home.to_numpy()
    S, J = n, len(r)
    plays = rng.random((S, J)) < r.p_play.to_numpy()[None, :]
    plays = _min_players(plays, r.p_play.to_numpy(), r.is_home.to_numpy())
    # stage 1: possessions, shooting shocks, script margin -> non-competitive time, OT
    h0 = r[home].iloc[0] if home.any() else r.iloc[0]
    mu_poss = float(h0.pace_own * h0.pace_opp / h0.league_pace)
    poss48 = rng.normal(mu_poss, sp["sd_pace"], S)
    shock = rng.normal(0, sp["sd_shoot"], (S, 2))  # [home, away]
    mu_h = float((r.p_play * r.pts_m)[home].sum())
    mu_a = float((r.p_play * r.pts_m)[~home].sum())
    script = ((mu_h * (1 + 1.6 * shock[:, 0]) - mu_a * (1 + 1.6 * shock[:, 1]))
              + rng.normal(0, sp["sd_script"], S))  # fmt: skip
    G = g_of(fit.g_curve, script)
    ot = (np.abs(script) < sp["ot_band"]).astype(float)
    ot = ot + ot * (rng.random(S) < 0.12)  # a second overtime about one time in eight
    # stage 2: minutes, possessions on court
    mult = (r.m_raw / np.maximum(r.c * (48 - r.G) + np.nan_to_num(r.g) * r.G, 1e-9)).fillna(
        1.0).to_numpy()  # rest/return multipliers carried from the means  # fmt: skip
    mins = np.zeros((S, J))
    side = np.where(home, 0, 1)
    cc, gg = r.c.to_numpy(), np.nan_to_num(r.g.to_numpy())
    for t in (0, 1):
        sel = side == t
        mins[:, sel] = _minutes(cc[sel], gg[sel], mult[sel], plays[:, sel], G, ot)
    poss = poss48 * (1 + 5 * ot / 48)
    on = poss[:, None] * mins / (48 + 5 * ot[:, None])
    # usage: shots per possession of the players on court sum to the team's level
    use = (r.fg2a + r.fg3a * r.f_fg3a + 0.44 * r.fta + r.tov * r.f_tov).to_numpy()
    team_use = r.use_team.to_numpy()
    u = np.ones((S, J))
    for t in (0, 1):
        sel = side == t
        tot = (mins[:, sel] / (48 + 5 * ot[:, None]) * use[sel][None, :]).sum(1)
        u[:, sel] = (team_use[sel][0] / np.maximum(tot, 1e-6))[:, None]
    # Shots in rounds: first shots (bounded by possessions, under-dispersed), then each
    # credited miss rebounded by the offence (log5) gives one more shot, up to 4 rounds. A
    # cold night therefore buys second chances, as on the floor. Twos/threes and players by
    # the weights; free throws Poisson; makes per player at his own percentage.
    from betdata.nba.engine.project import log5

    ks, fd = sp["k_shot"], sp["fga_disp"]
    a2 = np.zeros((S, J), dtype=np.int64)
    a3 = np.zeros((S, J), dtype=np.int64)
    m2 = np.zeros((S, J), dtype=np.int64)
    m3 = np.zeros((S, J), dtype=np.int64)
    fta = np.zeros((S, J), dtype=np.int64)
    hc = np.where(home, 1 + fit.home, 1 - fit.home) * sp["level"]
    sh = shock[:, side]  # (S, J)
    p2 = np.clip((r.fg2_pct * r.f_fg2p).to_numpy() * hc * (1 + sh), 0.01, 0.99)
    p3 = np.clip((r.fg3_pct * r.f_fg3p).to_numpy() * hc * (1 + sh), 0.01, 0.99)
    team = {}
    for t in (0, 1):
        sel = side == t
        x = r[sel].iloc[0]
        p_or = float(log5(x.oreb_rate, x.o_dreb_rate, x.lg_oreb))
        cr = float(x.credit)
        w2 = np.where(plays[:, sel], r.fg2a.to_numpy()[sel][None, :] * u[:, sel] * on[:, sel], 0)
        w3 = np.where(plays[:, sel], (r.fg3a * r.f_fg3a).to_numpy()[sel][None, :] * u[:, sel]
                      * on[:, sel], 0)  # fmt: skip
        wf = np.where(plays[:, sel], r.fta.to_numpy()[sel][None, :] * u[:, sel] * on[:, sel], 0)
        mu = w2.sum(1) + w3.sum(1)
        share3 = np.clip(w3.sum(1) / np.maximum(mu, 1e-9), 0, 1)
        pm = (w2 * p2[:, sel] + w3 * p3[:, sel]).sum(1) / np.maximum(mu, 1e-9)
        again = (1 - pm) * cr * p_or  # expected extra shots per shot
        mu0 = mu * (1 - again)
        n = np.maximum(0, np.rint(rng.normal(mu0, fd * np.sqrt(mu0)))).astype(np.int64)
        miss = np.zeros(S, dtype=np.int64)
        cred = np.zeros(S, dtype=np.int64)
        oreb = np.zeros(S, dtype=np.int64)
        for _ in range(4):
            n3 = rng.binomial(n, share3)
            x3 = _split(rng, n3, w3, ks)
            x2 = _split(rng, n - n3, w2, ks)
            y3 = rng.binomial(x3, p3[:, sel])
            y2 = rng.binomial(x2, p2[:, sel])
            a2[:, sel] += x2
            a3[:, sel] += x3
            m2[:, sel] += y2
            m3[:, sel] += y3
            mi = (x2 - y2 + x3 - y3).sum(1)
            c_ = rng.binomial(mi, cr)
            o_ = rng.binomial(c_, p_or)
            miss += mi
            cred += c_
            oreb += o_
            n = o_
            if not n.any():
                break
        fta[:, sel] = _split(rng, rng.poisson(wf.sum(1)), wf, ks)
        team[t] = {"miss": miss, "credited": cred, "oreb": oreb, "dreb_opp": cred - oreb}
    ftm = rng.binomial(fta, np.clip(r.ft_pct.to_numpy(), 0.01, 0.99)[None, :] * np.ones((S, 1)))
    pts = 2 * m2 + 3 * m3 + ftm
    for t in (0, 1):
        sel = side == t
        team[t]["fgm"] = (m2[:, sel] + m3[:, sel]).sum(1)
        team[t]["pts"] = pts[:, sel].sum(1)
    reb_o = np.zeros((S, J))
    reb_d = np.zeros((S, J))
    for t in (0, 1):
        sel, opp = side == t, side != t
        wo = np.where(plays[:, sel], (r.oreb.to_numpy()[sel])[None, :] * mins[:, sel], 0)
        reb_o[:, sel] = _split(rng, team[t]["oreb"], wo, sp["k_reb"])
        wd = np.where(plays[:, opp], (r.dreb.to_numpy()[opp])[None, :] * mins[:, opp], 0)
        reb_d[:, opp] = _split(rng, team[t]["dreb_opp"], wd, sp["k_reb"])
    ast = np.zeros((S, J))
    for t in (0, 1):
        sel = side == t
        x = r[sel].iloc[0]
        tast = rng.binomial(team[t]["fgm"], float(np.clip(x.ast_share, 0.01, 0.99)))
        wa = np.where(plays[:, sel], r.ast.to_numpy()[sel][None, :] * mins[:, sel], 0)
        ast[:, sel] = _split(rng, tast, wa, sp["k_ast"])
    reb = reb_o + reb_d
    nan = np.where(plays, 1.0, np.nan)
    draws = {"pts": pts * nan, "reb": reb * nan, "ast": ast * nan, "fg3m": m3 * nan,
             "pra": (pts + reb + ast) * nan, "minutes": mins * nan}
    checks = {
        "minutes": bool(np.allclose(mins.sum(1), 2 * (240 + 25 * ot))),
        "points": bool(np.array_equal(team[0]["pts"] + team[1]["pts"], pts.sum(1))),
        "rebounds": bool(all(np.array_equal(reb_o[:, side == t].sum(1) + reb_d[:, side != t]
                                            .sum(1), team[t]["credited"]) for t in (0, 1))
                         and all((team[t]["credited"] <= team[t]["miss"]).all()
                                 for t in (0, 1))),  # fmt: skip
        "assists": bool(all((ast[:, side == t].sum(1) <= team[t]["fgm"]).all() for t in (0, 1))),
        "player_le_team": bool(all((pts[:, side == t] <= team[t]["pts"][:, None]).all()
                                   for t in (0, 1))),  # fmt: skip
    }
    gid = int(r.game_id.iloc[0])
    return GameSim(gid, team[0]["pts"].astype(float), team[1]["pts"].astype(float), ot,
                   r[["player_id", "team_id", "is_home", "p_play"]].copy(), draws, checks)
