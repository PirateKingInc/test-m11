"""M17 B4 step 7 (and steps 1-3 at drive level): how drives end and what they are made of.

  outcome model   multinomial logit of a drive's result (td, fg attempt, punt, turnover, downs,
                  end of half, opponent TD, safety) on its start field position, the matchup's
                  expected points per play (offence + defence allowed - league, as-of), the
                  score, the clock and home field. Fitted on 2016-2019 drives.
  dropback curve  logistic share of a drive's plays that are dropbacks, on the score, the clock,
                  the field and the offence's neutral pass rate (its logit offset vs the
                  league): the engine's own expected-pass curve (no betting-line input).
  templates       2016-2019 drives grouped by (result, start bucket): plays, seconds, yards,
                  field goal distance and where the other team's next drive starts. The
                  simulation resamples them, so plays, time, yards and field position stay
                  jointly realistic.
  field goals     league make probability by distance (logistic, 2016-2019).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

OUTCOMES = ("td", "fg", "punt", "turnover", "downs", "end_half", "opp_td", "safety")
BUCKETS = np.array([0, 10, 20, 30, 40, 50, 60, 70, 75, 80, 90, 101])
TRAIN = (2016, 2017, 2018, 2019)


def outcome_of(result: pd.Series) -> pd.Series:
    return result.replace({"missed_fg": "fg"})


def bucket(start: np.ndarray) -> np.ndarray:
    return np.clip(np.searchsorted(BUCKETS, start, side="right") - 1, 0, len(BUCKETS) - 2)


def features(start, x, sd, half_sec, half, game_sec, home) -> np.ndarray:
    """Drive-level design: field position, matchup strength, score and clock."""
    s = np.asarray(start, float) / 100
    sdc = np.clip(np.asarray(sd, float), -21, 21) / 7
    hs = np.asarray(half_sec, float)
    late = np.where(np.asarray(half) >= 2, np.clip(1 - np.asarray(game_sec, float) / 1800, 0, 1),
                    0.0)  # fmt: skip
    x = np.asarray(x, float)
    h2 = np.asarray(half) >= 2
    sdr = np.asarray(sd, float)
    lead_late = h2 & (hs < 150) & (sdr > 0)  # the leader runs the clock out (kneels)
    trail_late = h2 & (hs < 150) & (sdr < 0)
    return np.column_stack([
        s, s * s, x, x * s, sdc, sdc * late, np.minimum(hs, 300) / 300, hs < 120, hs < 45,
        hs < 15, np.asarray(home, float), s * (hs < 120), lead_late, lead_late * (sdr <= 8),
        trail_late, trail_late * (sdr >= -8),
    ]).astype(float)  # fmt: skip


def pass_features(sd, half_sec, half, game_sec, start, offset) -> np.ndarray:
    sdc = np.clip(np.asarray(sd, float), -24, 24) / 7
    late = np.where(np.asarray(half) >= 2, np.clip(1 - np.asarray(game_sec, float) / 1800, 0, 1),
                    0.0)  # fmt: skip
    hs = np.asarray(half_sec, float)
    return np.column_stack([sdc, sdc * late, sdc * late * late, hs < 120, (hs < 120) * sdc,
                            np.asarray(start, float) / 100,
                            np.asarray(offset, float)]).astype(float)  # fmt: skip


@dataclass
class DriveModel:
    coef: np.ndarray  # (outcomes, features)
    intercept: np.ndarray
    pass_coef: np.ndarray
    pass_intercept: float
    fg_coef: tuple[float, float]  # logit(make) = a + b * distance
    templates: dict  # str(outcome, bucket) -> array rows [plays, seconds, yards, fg_dist,
    #                  next_start, dropback_share, start]
    pass_shift: np.ndarray = None  # per outcome: logit shift of the dropback share
    time_scale: float = 1.0  # template seconds x this: the game clock kick plays also use

    def probs(self, X: np.ndarray) -> np.ndarray:
        z = X @ self.coef.T + self.intercept
        z -= z.max(axis=1, keepdims=True)
        e = np.exp(z)
        return e / e.sum(axis=1, keepdims=True)

    def p_pass(self, F: np.ndarray, outcome: np.ndarray | None = None) -> np.ndarray:
        z = F @ self.pass_coef + self.pass_intercept
        if outcome is not None and self.pass_shift is not None:
            z = z + self.pass_shift[outcome]
        return 1 / (1 + np.exp(-z))

    def p_fg(self, dist: np.ndarray) -> np.ndarray:
        a, b = self.fg_coef
        return 1 / (1 + np.exp(-(a + b * np.asarray(dist, float))))

    def to_json(self) -> dict:
        return {"coef": self.coef.tolist(), "intercept": self.intercept.tolist(),
                "pass_coef": self.pass_coef.tolist(), "pass_intercept": self.pass_intercept,
                "fg_coef": list(self.fg_coef),
                "pass_shift": None if self.pass_shift is None else self.pass_shift.tolist(),
                "time_scale": self.time_scale,
                "templates": {k: v.tolist() for k, v in self.templates.items()}}  # fmt: skip

    @classmethod
    def from_json(cls, d: dict) -> DriveModel:
        return cls(np.array(d["coef"]), np.array(d["intercept"]), np.array(d["pass_coef"]),
                   float(d["pass_intercept"]), tuple(d["fg_coef"]),
                   {k: np.array(v) for k, v in d["templates"].items()},
                   None if d.get("pass_shift") is None else np.array(d["pass_shift"]),
                   float(d.get("time_scale", 1.0)))  # fmt: skip

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json()))

    @classmethod
    def load(cls, path: Path) -> DriveModel:
        return cls.from_json(json.loads(path.read_text()))


def matchup(r: pd.DataFrame, off: str = "", qb_delta=0.0) -> pd.Series:
    """Expected EPA per play of the offence against this defence, from as-of team rates."""
    xp = r.o_pass_epa + qb_delta + (r.d_pass_epa - r.l_pass_epa)
    xr = r.o_run_epa + (r.d_run_epa - r.l_run_epa)
    pr = r.o_neutral_pass
    return pr * xp + (1 - pr) * xr


def drive_frame(store, seasons) -> pd.DataFrame:
    """Drives of `seasons` with the as-of matchup of their game (rates as of 1 h before kickoff)."""
    dr = store.tables["drives"]
    dr = dr[dr.season.isin(seasons) & dr.result.notna() & dr.start_yl.notna()].copy()
    games = dr.groupby(["game_id", "posteam"]).agg(defteam=("defteam", "first"),
                                                   season=("season", "first"),
                                                   kickoff=("kickoff", "first")).reset_index()
    q = pd.DataFrame({"team": games.posteam, "opp": games.defteam, "season": games.season,
                      "t": games.kickoff - pd.Timedelta(hours=1)})  # fmt: skip
    r = store.team(q)
    games = pd.concat([games, r], axis=1)
    games["x"] = matchup(games)
    games["offset"] = _logit(games.o_neutral_pass) - _logit(games.l_neutral_pass)
    dr = dr.merge(games[["game_id", "posteam", "x", "offset"]], on=["game_id", "posteam"])
    dr["outcome"] = outcome_of(dr.result)
    return dr


def _logit(p):
    p = np.clip(np.asarray(p, float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def fit(store, seasons=TRAIN, C: float = 1.0) -> DriveModel:
    from sklearn.linear_model import LogisticRegression

    dr = drive_frame(store, seasons)
    dr = dr[dr.outcome.isin(OUTCOMES)]
    X = features(dr.start_yl, dr.x, dr.score_diff, dr.half_seconds, dr.half, dr.game_seconds,
                 dr.home)  # fmt: skip
    y = dr.outcome.map({o: i for i, o in enumerate(OUTCOMES)}).to_numpy()
    m = LogisticRegression(C=C, max_iter=2000).fit(X, y)
    coef = np.zeros((len(OUTCOMES), X.shape[1]))
    inter = np.full(len(OUTCOMES), -30.0)
    for j, cls in enumerate(m.classes_):
        coef[cls], inter[cls] = m.coef_[j], m.intercept_[j]
    # dropback share of a drive's plays (binomial via weights)
    d = dr[dr.plays > 0]
    F = pass_features(d.score_diff, d.half_seconds, d.half, d.game_seconds, d.start_yl, d.offset)
    Fp = np.vstack([F, F])
    yp = np.r_[np.ones(len(d)), np.zeros(len(d))]
    wp = np.r_[d.dropbacks.to_numpy(float), (d.plays - d.dropbacks).clip(lower=0).to_numpy(float)]
    pm = LogisticRegression(C=10.0, max_iter=2000).fit(Fp, yp, sample_weight=wp)
    # field goals by distance
    fg = dr[dr.fg_dist.notna()]
    fm = LogisticRegression(C=10.0, max_iter=1000).fit(fg[["fg_dist"]].to_numpy(float),
                                                       fg.fg_made.astype(int))  # fmt: skip
    m_out = DriveModel(coef, inter, pm.coef_[0], float(pm.intercept_[0]),
                       (float(fm.intercept_[0]), float(fm.coef_[0][0])), templates(dr))
    # dropback share given how the drive ended (3-and-outs pass more, TD drives less)
    pp = m_out.p_pass(F)
    shift = np.zeros(len(OUTCOMES))
    oc = d.outcome.map({o: i for i, o in enumerate(OUTCOMES)}).to_numpy()
    for i in range(len(OUTCOMES)):
        sel = oc == i
        if sel.sum() < 30:
            continue
        lo, hi = -3.0, 3.0  # solve sum(plays * p(z + s)) = sum(dropbacks) by bisection
        target = d.dropbacks.to_numpy(float)[sel].sum()
        pl = d.plays.to_numpy(float)[sel]
        z = _logit(pp[sel])
        for _ in range(50):
            mid = (lo + hi) / 2
            if (pl / (1 + np.exp(-(z + mid)))).sum() < target:
                lo = mid
            else:
                hi = mid
        shift[i] = (lo + hi) / 2
    m_out.pass_shift = shift
    return m_out


def templates(dr: pd.DataFrame) -> dict:
    out = {}
    dr = dr.assign(b=bucket(dr.start_yl.to_numpy()),
                   share=(dr.dropbacks / dr.plays.where(dr.plays > 0)).fillna(0.5))
    for (o, b), g in dr.groupby(["outcome", "b"]):
        arr = g[["plays", "seconds", "yards", "fg_dist", "next_start", "share",
                 "start_yl"]].to_numpy(float)  # fmt: skip
        out[f"{o}|{b}"] = arr
    return out


def kickoff_starts(store, t: pd.Timestamp, n: int = 600) -> np.ndarray:
    """Start field positions after kickoffs known at `t` (the last `n`): follows rule changes
    (2024 and 2025 kickoff rules) within weeks."""
    dr = store.tables["drives"]
    k = dr[(dr.transition == "KICKOFF") & (dr.known_at <= t) & dr.start_yl.notna()]
    k = k.sort_values("known_at").tail(n)
    return k.start_yl.to_numpy(float)


def pat_rates(store, t: pd.Timestamp, window: int = 544) -> dict:
    tg = store.team_games
    g = tg[tg.known_at <= t].sort_values("known_at").tail(window)
    td = g.d_td.sum()
    return {"xp": float(g.xp_made.sum() / max(g.xp_att.sum(), 1)),
            "two_rate": float(g.two_att.sum() / max(td, 1)),
            "two_made": float(g.two_made.sum() / max(g.two_att.sum(), 1))}  # fmt: skip
