"""M20 B1/B3/B4: per player-game and team-game facts from lineup stints and the box score.

From `nba_stints` (M15) the running margin at each stint's start is the cumulative stint points
of the earlier stints. A game whose stints do not cover it fully (a skipped period, M15
"Unresolved") is marked `complete = False`: its running margin is unknown after the gap, so it
has no non-competitive time and its players' shares come from the box minutes.

Competitive-minutes candidates (MODEL.md "M20 B3"); a stint is non-competitive when at its
start:
  cm1  |margin| >= 15 with <= 300 s left in regulation's fourth quarter;
  cm2  the leader's win probability > 0.95: Phi(|m| / (sigma * sqrt(left / 2880))), second
       half only (sigma fitted on training games, `fit_sigma`);
  cm3  |margin| >= 20 at any time in the fourth quarter.
Overtime is always competitive.

Everything here is a post-game fact: it is known with the game's box score (`known_at`).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import norm
from sqlalchemy import Connection, select

from betdata.db.schema import games, nba_stints

REG = 2880.0
DEFS = ("cm1", "cm2", "cm3")
SIGMA_DEFAULT = 13.0


def load_stints(conn: Connection, min_season: int | None = None,
                game_ids=None) -> pd.DataFrame:  # fmt: skip
    t = nba_stints
    q = (select(t.c.game_id, t.c.period, t.c.start_sec, t.c.end_sec, t.c.home_players,
                t.c.away_players, t.c.home_pts, t.c.away_pts, t.c.home_poss, t.c.away_poss,
                t.c.known_at)
         .join(games, games.c.game_id == t.c.game_id))  # fmt: skip
    if min_season is not None:
        q = q.where(games.c.season >= min_season)
    s = pd.read_sql(q, conn)
    if game_ids is not None:
        s = s[s.game_id.isin(set(game_ids))]
    return s.sort_values(["game_id", "start_sec"]).reset_index(drop=True)


def with_margin(s: pd.DataFrame) -> pd.DataFrame:
    """Adds `margin` (home - away at the stint's start), `left` (regulation seconds left, 0 in
    overtime), `ot` (stint in overtime) and per game `game_sec`, `covered`, `complete`."""
    s = s.copy()
    by = s.groupby("game_id", sort=False)
    s["margin"] = (by.home_pts.cumsum() - s.home_pts) - (by.away_pts.cumsum() - s.away_pts)
    s["final"] = by.home_pts.transform("sum") - by.away_pts.transform("sum")
    s["ot"] = s.period > 4
    s["left"] = np.where(s.ot, 0.0, np.clip(REG - s.start_sec, 0, REG))
    maxp = by.period.transform("max").clip(lower=4)
    s["game_sec"] = REG + 300.0 * (maxp - 4)
    dur = (s.end_sec - s.start_sec).clip(lower=0)
    s["dur"] = dur
    s["covered"] = s.groupby("game_id").dur.transform("sum")
    # complete: the stints tile the game (no skipped period; a few seconds of slack)
    gap = s.start_sec - by.end_sec.shift(1).fillna(0.0)
    s["_gap"] = gap.abs() > 1.0
    s["complete"] = (~s.groupby("game_id")._gap.transform("any")) & (
        (s.covered - s.game_sec).abs() <= 2.0)
    return s.drop(columns="_gap")


def fit_sigma(s: pd.DataFrame) -> float:
    """sigma of the score/time model: final - current margin ~ N(0, sigma^2 left/2880), from
    second-half stint starts of complete games (training seasons)."""
    d = s[s.complete & ~s.ot & (s.start_sec >= REG / 2) & (s.left >= 60)]
    if d.empty:
        return SIGMA_DEFAULT
    z2 = (d.final - d.margin) ** 2 / (d.left / REG)
    return float(np.sqrt(z2.mean()))


def noncompetitive(s: pd.DataFrame, sigma: float = SIGMA_DEFAULT) -> pd.DataFrame:
    """Boolean columns cm1..cm3 per stint (False in incomplete games and in overtime)."""
    m, left, q4 = s.margin.abs(), s.left, (s.period == 4)
    ok = s.complete & ~s.ot
    out = pd.DataFrame(index=s.index)
    out["cm1"] = ok & q4 & (left <= 300) & (m >= 15)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = m / (sigma * np.sqrt(np.maximum(left, 1.0) / REG))
    out["cm2"] = ok & (s.start_sec >= REG / 2) & (norm.cdf(z) > 0.95) & (m > 0)
    out["cm3"] = ok & q4 & (m >= 20)
    return out


def player_rows(s: pd.DataFrame, nc: pd.DataFrame) -> pd.DataFrame:
    """One row per (game, side, player) with on-court seconds, non-competitive seconds per
    definition and the end of his last stint."""
    parts = []
    for side in ("home", "away"):
        x = s[["game_id", f"{side}_players", "dur", "end_sec"]].join(nc)
        x = x.assign(player_id=x[f"{side}_players"].str.split(",")).explode("player_id")
        x["home_side"] = side == "home"
        parts.append(x.drop(columns=f"{side}_players"))
    x = pd.concat(parts, ignore_index=True)
    agg = {"sec_on": ("dur", "sum"), "last_on": ("end_sec", "max")}
    for d in DEFS:
        x[f"nc_{d}"] = x.dur.where(x[d], 0.0)
        agg[f"sec_nc_{d}"] = (f"nc_{d}", "sum")
    return x.groupby(["game_id", "home_side", "player_id"]).agg(**agg).reset_index()


def game_rows(s: pd.DataFrame, nc: pd.DataFrame) -> pd.DataFrame:
    x = s[["game_id", "dur", "game_sec", "complete", "final", "period"]].join(nc)
    agg = {"game_sec": ("game_sec", "first"), "complete": ("complete", "first"),
           "final_margin": ("final", "first"), "periods": ("period", "max")}
    for d in DEFS:
        x[f"g_{d}"] = x.dur.where(x[d], 0.0)
        agg[f"nc_sec_{d}"] = (f"g_{d}", "sum")
    return x.groupby("game_id").agg(**agg).reset_index()


def derive(stints: pd.DataFrame, sigma: float = SIGMA_DEFAULT) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(player-game rows, game rows) from stints."""
    if stints.empty:
        return (pd.DataFrame(columns=["game_id", "home_side", "player_id", "sec_on", "last_on"]),
                pd.DataFrame(columns=["game_id", "game_sec", "complete"]))  # fmt: skip
    s = with_margin(stints)
    nc = noncompetitive(s, sigma)
    return player_rows(s, nc), game_rows(s, nc)
