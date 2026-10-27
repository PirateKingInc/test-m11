"""M17: run the engine over history (both prediction points), as of each point: game-level
summaries (margin, total, win probability, team volumes) and player-level summaries (mean,
median, quantiles, P(anytime TD)), saved per season for evaluation. No line is read.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from betdata.nfl.engine import avail as A
from betdata.nfl.engine import drives as D
from betdata.nfl.engine import inputs as I
from betdata.nfl.engine import plays as P
from betdata.nfl.engine import rates as R
from betdata.nfl.engine import sim as SM

log = logging.getLogger(__name__)
POINTS = ("final_report", "prekick")
PLAYER_STATS = ("targets", "receptions", "rec_yards", "rec_td", "carries", "rush_yards",
                "rush_td", "pass_yards", "pass_attempts")  # fmt: skip
TEAM_STATS = ("plays", "dropbacks", "sacks", "scrambles", "attempts", "runs", "pass_yards",
              "run_yards", "drives", "td", "fg_made", "pass_td", "rush_td")  # fmt: skip


@dataclass
class EngineConfig:
    rates: R.RateParams = field(default_factory=R.RateParams)
    sim: SM.SimParams = field(default_factory=SM.SimParams)
    fit_seasons: tuple = D.TRAIN

    def key(self) -> str:
        import hashlib
        import json

        return hashlib.sha1(json.dumps(asdict(self), sort_keys=True, default=str).encode()
                            ).hexdigest()[:10]  # fmt: skip


class Engine:
    """Loaded once: tables, rate store, drive model, availability, role priors."""

    def __init__(self, cfg: EngineConfig | None = None, seasons=range(2016, 2027),
                 model: D.DriveModel | None = None):  # fmt: skip
        self.cfg = cfg or EngineConfig()
        self.store = R.RateStore(P.load(seasons), self.cfg.rates)
        self.model = model or D.fit(self.store, self.cfg.fit_seasons)
        self.avail = A.load(seasons)
        self.ids = I.team_ids()
        self.coefs = I.role_coefs(self.store, self.avail, self.ids, self.cfg.fit_seasons)

    def slots(self, seasons, points=POINTS) -> list[I.Slot]:
        from betdata.nfl.points import point_time

        tg = self.store.team_games
        g = tg[tg.season.isin(seasons)].drop_duplicates("game_id").sort_values("kickoff")
        out = []
        for r in g.itertuples():
            h = self.ids.loc[r.game_id]
            for pt in points:
                t = pd.Timestamp(point_time(pt, r.kickoff.to_pydatetime()))
                out.append(I.Slot(r.game_id, pt, t, int(r.season), h.home, h.away))
        return out

    def run(self, slots: list[I.Slot], players: bool = True, seed: int = 0):
        tr = I.team_rows(self.store, slots)
        pr = I.player_rows(self.store, self.avail, slots, self.ids, self.coefs)
        games, plist = [], []
        for i, s in enumerate(slots):
            t0 = time.perf_counter()
            home, away = I.sides(tr, pr, s, self.cfg.rates.qb_weight)
            gs = SM.simulate(self.model, home, away, D.kickoff_starts(self.store, s.t),
                             D.pat_rates(self.store, s.t), self.cfg.sim, seed + i)  # fmt: skip
            games.append(summarise_game(s, gs, time.perf_counter() - t0))
            if players:
                plist.extend(summarise_players(s, gs, home, away))
        return pd.DataFrame(games), pd.DataFrame(plist)


def summarise_game(s: I.Slot, gs: SM.GameSim, secs: float) -> dict:
    m, tot = gs.margin, gs.home_pts + gs.away_pts
    out = dict(game_id=s.game_id, point=s.point, season=s.season, home=s.home, away=s.away,
               margin_mean=float(m.mean()), margin_median=float(np.median(m)),
               margin_p10=float(np.percentile(m, 10)), margin_p90=float(np.percentile(m, 90)),
               total_mean=float(tot.mean()), total_median=float(np.median(tot)),
               total_p10=float(np.percentile(tot, 10)), total_p90=float(np.percentile(tot, 90)),
               p_home_win=float((m > 0).mean() + 0.5 * (m == 0).mean()),
               home_pts=float(gs.home_pts.mean()), away_pts=float(gs.away_pts.mean()),
               sim_seconds=secs)  # fmt: skip
    for (side, k), v in gs.team.items():
        if k in TEAM_STATS:
            out[f"{'h' if side == 0 else 'a'}_{k}"] = float(v.mean())
    # P(home covers) at every half-point line from -20.5 to 20.5 is recomputed from the
    # margin distribution when needed; store a compact histogram instead of the draws.
    out["margin_hist"] = np.bincount(np.clip(m.astype(int) + 60, 0, 120), minlength=121)
    out["total_hist"] = np.bincount(np.clip(tot.astype(int), 0, 120), minlength=121)
    return out


def summarise_players(s: I.Slot, gs: SM.GameSim, home: SM.Side, away: SM.Side) -> list[dict]:
    out = []
    ids = {pid for (pid, _k) in gs.players}
    team_of = {}
    for side, team in ((home, s.home), (away, s.away)):
        for r in side.rec + side.rush:
            team_of[r[0]] = team
        for q in side.qbs:
            team_of[q[0]] = team
    for pid in ids:
        row = dict(game_id=s.game_id, point=s.point, season=s.season, player=pid,
                   team=team_of.get(pid))  # fmt: skip
        played = gs.players.get((pid, "played"))
        row["p_play"] = float(np.mean(played)) if played is not None else None
        for k in PLAYER_STATS:
            v = gs.players.get((pid, k))
            if v is None or np.ndim(v) == 0:
                continue
            v = np.asarray(v, float)
            row[f"{k}_mean"] = float(v.mean())
            row[f"{k}_median"] = float(np.median(v))
            if k in ("rec_yards", "rush_yards", "pass_yards", "receptions"):
                row[f"{k}_q"] = np.percentile(v, np.arange(5, 100, 5)).astype(np.float32)
        tds = np.asarray(gs.players.get((pid, "rec_td"), 0), float) + np.asarray(
            gs.players.get((pid, "rush_td"), 0), float)
        row["p_anytime_td"] = float(np.mean(tds >= 1)) if np.ndim(tds) else 0.0
        out.append(row)
    return out


def actuals(store: R.RateStore) -> tuple[pd.DataFrame, pd.DataFrame]:
    tg = store.team_games
    h = tg[tg.is_home].set_index("game_id")
    games = pd.DataFrame({"act_margin": h.points - h.opp_points,
                          "act_total": h.points + h.opp_points})
    pl = store.players.set_index(["game_id", "player"])
    pl = pl[~pl.index.duplicated()]
    return games, pl


def run_seasons(eng: Engine, seasons, out: Path | None = None, players: bool = True,
                points=POINTS) -> tuple[pd.DataFrame, pd.DataFrame]:  # fmt: skip
    gs, ps = [], []
    for season in seasons:
        t0 = time.time()
        g, p = eng.run(eng.slots([season], points), players=players)
        log.info("engine %s: %d slots in %.0fs", season, len(g), time.time() - t0)
        gs.append(g)
        ps.append(p)
        if out is not None:
            out.mkdir(parents=True, exist_ok=True)
            g.to_pickle(out / f"games_{season}.pkl")
            if players:
                p.to_pickle(out / f"players_{season}.pkl")
    return pd.concat(gs, ignore_index=True), pd.concat(ps, ignore_index=True)


def calibrate(eng: Engine, seasons=(2018, 2019), n: int = 500, rounds: int = 3) -> dict:
    """Training-season calibration of two clock/scoring constants, fitted to 2016-2019 games
    only (never validation or test):
      time_scale  template seconds x this, so simulated drives per team-game match the real
                  ones (drive time of possession leaves out kickoffs, punts and kicks)
      td_shift    added to the touchdown logit, so simulated points per team-game match
      pass shift  added to the dropback curve, so the simulated dropback share matches
    """
    import copy

    tg = eng.store.team_games
    real = tg[tg.season.isin(seasons)]
    target_drives, target_pts = real.drives.mean(), real.points.mean()
    target_db = real.dropbacks.sum() / real.plays.sum()
    slots = [s for s in eng.slots(seasons, ("final_report",))][::2]
    cfg = copy.deepcopy(eng.cfg)
    cfg.sim.n = n
    saved = eng.cfg
    eng.cfg = cfg
    log_ = []
    try:
        for _ in range(rounds):
            g, _p = eng.run(slots, players=False)
            drives = (g.h_drives.mean() + g.a_drives.mean()) / 2
            pts = (g.home_pts.mean() + g.away_pts.mean()) / 2
            db = (g.h_dropbacks.sum() + g.a_dropbacks.sum()) / (g.h_plays.sum() + g.a_plays.sum())
            log_.append(dict(time_scale=eng.model.time_scale,
                             td_intercept=float(eng.model.intercept[0]),
                             pass_intercept=eng.model.pass_intercept, drives=drives, pts=pts,
                             dropback_share=db))  # fmt: skip
            eng.model.time_scale *= drives / target_drives
            eng.model.intercept[0] += np.log(target_pts / pts) * 1.6
            eng.model.pass_intercept += float(np.log(target_db / (1 - target_db))
                                              - np.log(db / (1 - db)))  # fmt: skip
    finally:
        eng.cfg = saved
    return {"target_drives": float(target_drives), "target_points": float(target_pts),
            "target_dropback_share": float(target_db),
            "rounds": log_, "time_scale": eng.model.time_scale,
            "td_intercept": float(eng.model.intercept[0])}
