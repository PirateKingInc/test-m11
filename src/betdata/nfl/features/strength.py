"""Group 2: team strength.

Opponent-adjusted unit ratings: for each per-play metric, every known team-game (offense o
against defense d) is a row of a weighted ridge regression  y = mu + off_o + def_d + home,
weighted by plays and by recency (half-life in days, measured back from the latest known game,
so the ratings depend only on what is known). Metrics: pass and rush EPA per play, success
rate, early-down EPA, explosive-play rate. A game's matchup feature is (home offense + away
defense) minus (away offense + home defense) for the margin, and the sum for the total. Between
seasons the ratings shrink toward average.

Also: turnover luck (fumble recoveries vs 50%, interceptions thrown and caught vs the league),
pace and neutral-situation pass rate (totals), and preseason priors that fade over the first
eight games: a new head coach, a new starting QB.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from betdata.nfl.store import AsOf

METRICS = {
    # name: (numerator, denominator)
    "pass": ("pass_epa", "pass_plays"),
    "rush": ("rush_epa", "rush_plays"),
    "success": ("succ", "plays"),
    "early": ("early_epa", "early_plays"),
    "explosive": ("explosive", "plays"),
}
HALF_LIFE_DAYS = 120.0
LOOKBACK_DAYS = 600
RIDGE_PLAYS = 250.0  # prior weight, in plays, pulling each unit toward average
OFFSEASON_KEEP = 0.7  # share of a rating kept into the next season
PRIOR_GAMES = 8  # preseason priors fade out over this many games
NS_PER_DAY = 86400 * 10**9


def _ratings(tg: pd.DataFrame) -> dict[str, dict]:
    """{metric: {"off": {team: r}, "def": {team: r}, "mu": m}} from known team-games."""
    if tg.empty:
        return {m: {"off": {}, "def": {}, "mu": 0.0} for m in METRICS}
    latest = tg["kickoff"].max()
    tg = tg[tg["kickoff"] >= latest - pd.Timedelta(days=LOOKBACK_DAYS)]
    age = (latest - tg["kickoff"]).dt.total_seconds().to_numpy() / 86400
    decay = 0.5 ** (age / HALF_LIFE_DAYS)
    teams = sorted(set(tg["team"]) | set(tg["opp"]))
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    oi = tg["team"].map(idx).to_numpy()
    di = tg["opp"].map(idx).to_numpy()
    home = np.where(tg["neutral"].to_numpy(dtype=bool), 0.0,
                    np.where(tg["home"].to_numpy(dtype=bool), 1.0, -1.0))  # fmt: skip
    out = {}
    succ = tg["pass_succ"] + tg["rush_succ"]
    for m, (num, den) in METRICS.items():
        top = succ if num == "succ" else tg[num]
        plays = tg[den].to_numpy(dtype=float)
        ok = plays > 0
        y = np.divide(top.to_numpy(dtype=float), plays, out=np.zeros_like(plays), where=ok)
        w = plays * decay * ok
        mu = float((w * y).sum() / w.sum()) if w.sum() else 0.0
        X = np.zeros((len(y), 2 * n + 1))
        X[np.arange(len(y)), oi] = 1.0
        X[np.arange(len(y)), n + di] = 1.0
        X[:, 2 * n] = home
        A = (X * w[:, None]).T @ X + RIDGE_PLAYS * np.diag([1.0] * (2 * n) + [0.0])
        A[2 * n, 2 * n] += 1e-6
        b = (X * w[:, None]).T @ (y - mu)
        coef = np.linalg.solve(A, b)
        out[m] = {"off": {t: coef[i] for t, i in idx.items()},
                  "def": {t: coef[n + i] for t, i in idx.items()}, "mu": mu}  # fmt: skip
    return out


def _team_rates(tg: pd.DataFrame) -> dict[int, dict[str, float]]:
    """Recency-weighted team habits and luck (not opponent-adjusted)."""
    if tg.empty:
        return {}
    latest = tg["kickoff"].max()
    tg = tg[tg["kickoff"] >= latest - pd.Timedelta(days=LOOKBACK_DAYS)].copy()
    tg["w"] = 0.5 ** ((latest - tg["kickoff"]).dt.total_seconds() / 86400 / HALF_LIFE_DAYS)
    league_int = (tg.ints * tg.w).sum() / max((tg.pass_att * tg.w).sum(), 1.0)
    # Interceptions a defense caught = the opponent's interceptions thrown in that game.
    caught = tg[["game_id", "team", "ints", "pass_att"]].merge(
        tg[["game_id", "opp", "w"]].rename(columns={"opp": "team"}), on=["game_id", "team"]
    )
    caught = caught.rename(columns={"team": "passer"})
    by_game_opp = tg.set_index(["game_id", "team"])["opp"]
    caught["team"] = [by_game_opp.get((g, t)) for g, t in zip(caught.game_id, caught.passer,
                                                              strict=True)]  # fmt: skip
    out = {}
    for team, d in tg.groupby("team"):
        w = d.w
        c = caught[caught.team == team]
        fumbles = (d.game_fumbles * w).sum()
        out[team] = {
            "secs": (d.neutral_secs * w).sum() / max((d.neutral_secs_n * w).sum(), 1.0),
            "npass": (d.neutral_pass * w).sum() / max((d.neutral_plays * w).sum(), 1.0),
            "fum_luck": ((d.fumble_recoveries * w).sum() - 0.5 * fumbles) / max(w.sum(), 1.0),
            "int_off": (d.ints * w).sum() / max((d.pass_att * w).sum(), 1.0) - league_int,
            "int_def": (c.ints * c.w).sum() / max((c.pass_att * c.w).sum(), 1.0) - league_int,
        }
    return out


def _season_info(tg: pd.DataFrame, pg: pd.DataFrame, coaches: pd.DataFrame, games):
    """Per (season, team): games played so far, primary QB, head coach of the last game."""
    played = tg.groupby(["season", "team"]).size().to_dict()
    qb = {}
    if len(pg):
        d = pg[pg.dropbacks > 0].groupby(["season", "team", "key"])["dropbacks"].sum()
        for (season, team), best in d.groupby(level=[0, 1]).idxmax().items():
            qb[(season, team)] = best[2]
    coach = {}
    if len(coaches):
        c = coaches.merge(games[["season", "kickoff", "home", "away"]], left_on="game_id",
                          right_index=True).sort_values("kickoff")  # fmt: skip
        for r in c.itertuples(index=False):
            coach[(r.season, r.home)] = r.home_coach
            coach[(r.season, r.away)] = r.away_coach
    return played, qb, coach


class Strength:
    name = "str"
    version = 1

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        tg = a.df("team_games")
        ratings = a.cached("str_ratings", ("team_games",), lambda: _ratings(tg))
        rates = a.cached("str_rates", ("team_games",), lambda: _team_rates(tg))
        info = a.cached("str_info", ("team_games", "player_games", "coaches"),
                        lambda: _season_info(tg, a.df("player_games"), a.df("coaches"),
                                             a.games))  # fmt: skip
        played, qb, coach = info
        latest_season = int(tg["season"].max()) if len(tg) else g.season
        keep = OFFSEASON_KEEP if g.season > latest_season else 1.0
        h, w = g.home, g.away
        out: dict[str, float] = {}
        for m, r in ratings.items():
            off, dfn = r["off"], r["def"]
            eh = keep * (off.get(h, 0.0) + dfn.get(w, 0.0))
            ea = keep * (off.get(w, 0.0) + dfn.get(h, 0.0))
            out[f"m_str_{m}"] = eh - ea
            out[f"t_str_{m}"] = eh + ea
        rh, rw = rates.get(h, {}), rates.get(w, {})
        for k in ("fum_luck", "int_off", "int_def"):
            out[f"m_str_{k}"] = rh.get(k, 0.0) - rw.get(k, 0.0)
            out[f"t_str_{k}"] = rh.get(k, 0.0) + rw.get(k, 0.0)
        out["t_str_secs"] = rh.get("secs", 27.0) + rw.get("secs", 27.0)
        out["t_str_npass"] = rh.get("npass", 0.55) + rw.get("npass", 0.55)

        # Preseason priors, fading over the first PRIOR_GAMES games of the season.
        coaches = a.df("coaches")
        this = coaches[coaches.game_id == g.game_id]
        now_coach = ({"h": this.home_coach.iloc[0], "w": this.away_coach.iloc[0]}
                     if len(this) else {})  # fmt: skip
        for side, team in (("h", h), ("w", w)):
            fade = max(0.0, 1 - played.get((g.season, team), 0) / PRIOR_GAMES)
            last_coach = coach.get((g.season - 1, team))
            new_coach = float(bool(now_coach) and last_coach is not None
                              and now_coach[side] != last_coach)  # fmt: skip
            prev_qb, cur_qb = qb.get((g.season - 1, team)), qb.get((g.season, team))
            new_qb = float(prev_qb is not None and cur_qb is not None and cur_qb != prev_qb)
            out[f"_{side}_coach"] = fade * new_coach
            out[f"_{side}_qb"] = fade * new_qb
        for k in ("coach", "qb"):
            hv, wv = out.pop(f"_h_{k}"), out.pop(f"_w_{k}")
            out[f"m_str_new_{k}"] = hv - wv
            out[f"t_str_new_{k}"] = hv + wv
        return out
