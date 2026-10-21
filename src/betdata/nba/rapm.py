"""M15 lineup RAPM (MODEL.md, "M15 NBA side variables" 1): a ridge regression of the home
lineup's point margin per 100 possessions on the ten players on the floor, fitted weekly on
stints of games known before each snapshot (current and previous season, the previous one at
half weight), and the team strength rebuilt from expected available players' values.

A snapshot at time T uses only stints whose game was known before T; a prediction at instant t
reads the latest snapshot at or before t. Players with few minutes are blended toward a
box-score prior (game score per 36 above replacement, M14's player value) by minutes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from scipy.sparse.linalg import lsqr
from sqlalchemy import Connection, select

from betdata.db.schema import nba_stints
from betdata.nba.features import _state, play_prob, status_key, streak_for, team_status
from betdata.nfl.store import AsOf

LAMBDA = 3000.0  # ridge penalty in possession-weighted units
PRIOR_MIN = 250.0  # minutes at which RAPM and the box prior weigh equally
PREV_WEIGHT = 0.5
BOX_SCALE = 0.6  # box prior: points per 100 possessions per unit of game score per 36


def load_stints(conn: Connection, min_season: int | None = None) -> pd.DataFrame:
    from betdata.db.schema import games

    t = nba_stints
    q = (select(t.c.game_id, t.c.start_sec, t.c.end_sec, t.c.home_players, t.c.away_players,
                t.c.home_pts, t.c.away_pts, t.c.home_poss, t.c.away_poss, t.c.known_at,
                games.c.season)
         .join(games, games.c.game_id == t.c.game_id)
         .where(games.c.season >= (min_season or 0)))  # fmt: skip
    df = pd.read_sql(q, conn)
    df["known_at"] = pd.to_datetime(df.known_at, utc=True)
    df["poss"] = (df.home_poss + df.away_poss) / 2
    df["minutes"] = (df.end_sec - df.start_sec) / 60
    return df[df.poss > 0].reset_index(drop=True)


def fit_snapshot(st: pd.DataFrame, season: int, lam: float = LAMBDA) -> dict[str, tuple]:
    """player -> (net RAPM per 100 possessions, minutes in the window)."""
    w = np.where(st.season == season, 1.0, PREV_WEIGHT) * st.poss.to_numpy()
    homes = st.home_players.str.split(",")
    aways = st.away_players.str.split(",")
    players = sorted(set(p for lst in homes for p in lst) | set(p for lst in aways for p in lst))
    idx = {p: i for i, p in enumerate(players)}
    rows, cols, vals = [], [], []
    for r, (h, a) in enumerate(zip(homes, aways, strict=True)):
        for p in h:
            rows.append(r)
            cols.append(idx[p])
            vals.append(1.0)
        for p in a:
            rows.append(r)
            cols.append(idx[p])
            vals.append(-1.0)
    n = len(st)
    k = len(players)
    # Home intercept as the last column.
    rows += list(range(n))
    cols += [k] * n
    vals += [1.0] * n
    X = csr_matrix((vals, (rows, cols)), shape=(n, k + 1))
    y = 100 * (st.home_pts - st.away_pts).to_numpy() / st.poss.to_numpy()
    sw = np.sqrt(w)
    Xw = X.multiply(sw[:, None]).tocsr()
    coef = lsqr(Xw, y * sw, damp=np.sqrt(lam))[0]
    mins = {}
    for lst, m in ((homes, st.minutes), (aways, st.minutes)):
        for ps, mm in zip(lst, m, strict=True):
            for p in ps:
                mins[p] = mins.get(p, 0.0) + mm
    return {p: (float(coef[i]), mins.get(p, 0.0)) for p, i in idx.items()}


def mondays(start, end) -> list[pd.Timestamp]:
    t = pd.Timestamp(start).tz_convert("UTC").normalize()
    t = t + pd.Timedelta(days=(7 - t.weekday()) % 7) + pd.Timedelta(hours=12)
    out = []
    while t <= end:
        out.append(t)
        t += pd.Timedelta(days=7)
    return out


def snapshots(stints: pd.DataFrame, times, lam: float = LAMBDA) -> dict:
    """{snapshot time: {player: (rapm, minutes)}} for each time, from stints known before it in
    that season and the previous one."""
    out = {}
    for t in times:
        season = int(stints.season[stints.known_at < t].max()) if (stints.known_at < t).any() \
            else None
        if season is None:
            continue
        st = stints[(stints.known_at < t) & stints.season.isin((season - 1, season))]
        if len(st) < 2000:
            continue
        out[t] = fit_snapshot(st, season, lam)
    return out


def value(snap: dict, pid: str, gmsc36: float) -> float:
    """RAPM blended with the box prior by minutes (per 100 possessions)."""
    r, m = snap.get(pid, (0.0, 0.0))
    prior = BOX_SCALE * gmsc36
    w = m / (m + PRIOR_MIN)
    return w * r + (1 - w) * prior


@dataclass(frozen=True)
class Rapm:
    name: str = "rapm"

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        snaps = a.store.params.get("rapm") or {}
        times = [t for t in snaps if t <= a.t]
        if not times:
            return {"m_rapm_net": 0.0, "m_rapm_has": 0.0}
        snap = snaps[max(times)]
        st = _state(a)
        out = {}
        for side, team in (("h", g.home), ("a", g.away)):
            status, reported = team_status(a, g, team)
            tot, val = 0.0, 0.0
            for pid, p in st.rotation(team):
                prob = play_prob(a, status_key(status.get(pid), streak_for(st, g, p), reported))
                m = prob * p.min_play
                tot += m
                val += m * value(snap, pid, p.value()[0])
            out[side] = val / tot * 5 if tot else 0.0  # five on the floor, per 100 poss
        pace = st.league_pace / 100
        return {"m_rapm_net": (out["h"] - out["a"]) * pace, "m_rapm_has": 1.0}


def snapshot_times(store) -> list:
    g = store.games
    return mondays(g.tip.min() + timedelta(days=30), g.tip.max() + timedelta(days=1))
