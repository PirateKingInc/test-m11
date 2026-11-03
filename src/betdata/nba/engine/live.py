"""M20 Part G: the engine live, in the background (MODEL.md "M20: Part G").

The hourly run predicts the next two days' NBA games with the frozen fit (never refitted
hourly), simulates each 10,000 times as of now, and writes `data/derived/nba_engine_live.json`:
per game the margin and total distributions, per player the distribution of each prop stat.
The poller reads it to price DK/FD lines (picks.py); after a qualifying injury change it
re-simulates that game with the new statuses."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import Connection

from betdata.nba.engine import VERSION
from betdata.nba.engine import sim as SM

log = logging.getLogger(__name__)
FIT_PATH = Path(__file__).resolve().parents[4] / "config" / "fits" / "nba_engine_m20.json"
LIVE_PATH = Path("data/derived/nba_engine_live.json")
DAYS = 2
STATS = ("pts", "reb", "ast", "fg3m", "pra")


def load_fit(path: Path = FIT_PATH):
    from betdata.nba.engine.project import Fit

    raw = json.loads(path.read_text())
    return Fit.from_json(raw["fit"]), raw


def build_engine(conn: Connection, now: datetime, fit=None):
    """The engine over the live window (this season and the last), as of now."""
    from betdata.core.matching import season_for
    from betdata.nba.data import load_injuries
    from betdata.nba.engine import data as E
    from betdata.nba.engine import fitting as F

    if fit is None:
        fit, _ = load_fit()
    season = season_for("nba", now)
    sigma = float((fit.sim or {}).get("sigma_wp", 16.33))
    g, p, tg = E.load(conn, season - 1, sigma)
    inj = load_injuries(conn, g)
    return F.Engine(F.Inputs(g, p, tg, inj), fit)


def upcoming(eng, now: datetime, days: int = DAYS) -> list[int]:
    g = eng.inp.g
    t = pd.Timestamp(now)
    sel = (g.status == "scheduled") & (g.tip > t) & (g.tip <= t + pd.Timedelta(days=days))
    return [int(x) for x in g[sel].game_id]


def slots_now(eng, gids, now: datetime) -> pd.DataFrame:
    from betdata.nba.engine.project import slots_for

    s = slots_for(eng.inp.g, gids, ("pretip",))
    s["t"] = pd.Timestamp(now)
    s["point"] = "live"
    return s


def _cdf(x: np.ndarray) -> list[float]:
    """P(X <= k) for k = 0..max, rounded (the poller prices any line from it)."""
    x = x[~np.isnan(x)].astype(int)
    if len(x) == 0:
        return []
    counts = np.bincount(np.clip(x, 0, 200))
    return [round(float(v), 5) for v in np.cumsum(counts) / len(x)]


def _hist(x: np.ndarray, lo: int, hi: int) -> dict:
    v = np.clip(np.rint(x).astype(int), lo, hi)
    c = np.bincount(v - lo, minlength=hi - lo + 1)
    return {"lo": lo, "p": [round(float(q), 5) for q in c / len(x)]}


def game_doc(s: SM.GameSim, rows: pd.DataFrame, names: dict, label: str, tip) -> dict:
    players = []
    for j, r in enumerate(rows.reset_index(drop=True).itertuples()):
        st = {k: _cdf(s.draws[k][:, j]) for k in STATS}
        dist = {k: s.player_dist(j, k) for k in STATS}
        mins = s.player_dist(j, "minutes")
        players.append({"player_id": str(r.player_id), "name": names.get(str(r.player_id), ""),
                        "team_id": int(r.team_id), "is_home": bool(r.is_home),
                        "p_play": round(float(r.p_play), 4),
                        "status": r.status if isinstance(r.status, str) else None,
                        "minutes": None if mins is None else round(mins["mean"], 2),
                        "role_change": bool(r.role_change),
                        "median": {k: (None if dist[k] is None else dist[k]["median"])
                                   for k in STATS},
                        "mean": {k: (None if dist[k] is None else round(dist[k]["mean"], 3))
                                 for k in STATS},
                        "cdf": st})  # fmt: skip
    return {"game_id": int(s.game_id), "label": label, "tip": pd.Timestamp(tip).isoformat(),
            "mu_margin": round(float(s.margin.mean()), 3),
            "mu_total": round(float(s.total.mean()), 3), "p_home_win": round(s.p_home_win(), 4),
            "margin": _hist(s.margin, -70, 70), "total": _hist(s.total, 140, 320),
            "checks": s.checks, "players": players}


def predict_live(conn: Connection, now: datetime, eng=None, gids=None,
                 n_sims: int = SM.N_SIMS) -> dict:  # fmt: skip
    eng = eng or build_engine(conn, now)
    gids = upcoming(eng, now) if gids is None else gids
    doc = {"version": VERSION, "generated_at": now.isoformat(), "n_sims": n_sims, "games": []}
    if not gids:
        return doc
    c = eng.frame(gids, slots=slots_now(eng, gids, now))
    d = eng.means(c)
    p = eng.inp.p
    names = dict(zip(p.player_id.astype(str), p.player_name, strict=False))
    g = eng.inp.g
    for gid, rows in d.groupby("game_id"):
        s = SM.sim_game(rows, eng.fit, n_sims, seed=int(gid))
        label = f"{g.at[gid, 'away_abbr']} @ {g.at[gid, 'home_abbr']}"
        doc["games"].append(game_doc(s, rows, names, label, g.at[gid, "tip"]))
    return doc


def run(engine, now: datetime, path: Path = LIVE_PATH) -> int:
    """The hourly run's step: write the live document (never raises into the run)."""
    if not FIT_PATH.exists():
        log.info("nba engine: no frozen fit yet")
        return 0
    with engine.connect() as conn:
        doc = predict_live(conn, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, separators=(",", ":")))
    log.info("nba engine: %d games predicted", len(doc["games"]))
    return len(doc["games"])


def read(path: Path = LIVE_PATH, now: datetime | None = None,
         max_age: timedelta = timedelta(hours=3)) -> dict | None:  # fmt: skip
    if not path.exists():
        return None
    doc = json.loads(path.read_text())
    if now is not None and now - datetime.fromisoformat(doc["generated_at"]) > max_age:
        return None
    return doc


def p_over(cdf: list[float], line: float) -> tuple[float, float] | None:
    """(P(over), P(push)) from a stored CDF."""
    if not cdf:
        return None
    k = int(np.floor(line))

    def F(i):
        return 0.0 if i < 0 else (cdf[i] if i < len(cdf) else 1.0)

    if float(line).is_integer():
        return 1 - F(k), F(k) - F(k - 1)
    return 1 - F(k), 0.0


def p_hist_above(h: dict, x: float) -> tuple[float, float]:
    """(P(value > x), P(value == x)) from a stored histogram."""
    lo, p = h["lo"], np.asarray(h["p"])
    vals = lo + np.arange(len(p))
    return float(p[vals > x].sum()), float(p[vals == x].sum())
