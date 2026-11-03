"""M20 engine inputs: one row per player-game (box line + stints-derived facts) and one per
team-game, every row with its `known_at` (the box score's). No line table is read here.

Rates are per on-court team possession over the full game (MODEL.md "M20 B1"): a player's
on-court possessions = his team's possessions x his minutes / game minutes. Team possessions
are the box formula FGA - OREB + TOV + 0.44 FTA.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import Connection, select

from betdata.db.schema import games, nba_player_games
from betdata.nba.data import _utc, load_games
from betdata.nba.engine import derive as D

BOX = ("minutes", "points", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast",
       "stl", "blk", "tov", "pf")  # fmt: skip
RATES = ("fg2a", "fg3a", "fta", "oreb", "dreb", "ast", "tov")  # per on-court possession
PCTS = {"fg2": ("fg2m", "fg2a"), "fg3": ("fg3m", "fg3a"), "ft": ("ftm", "fta")}
BLOWOUT_MIN = 3.0  # B4: a game with >= 3 non-competitive minutes is a blowout (CM1 spans 5)


Frames = tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]


def load(conn: Connection, min_season: int | None = None,
         sigma: float = D.SIGMA_DEFAULT) -> Frames:
    """(games, player-games, team-games). Player-games include did-not-play rows
    (`played` False)."""
    g = load_games(conn, min_season)
    t = nba_player_games
    p = pd.read_sql(
        select(t.c.game_id, t.c.team_id, t.c.player_id, t.c.player_name, t.c.starter,
               t.c.did_not_play, t.c.known_at, *(t.c[c] for c in BOX))
        .join(games, games.c.game_id == t.c.game_id)
        .where(games.c.season >= (min_season or 0)), conn)  # fmt: skip
    p = p[p.game_id.isin(g.index)].copy()
    p[list(BOX)] = p[list(BOX)].astype(float).fillna(0.0)
    p["starter"] = p.starter.fillna(False).astype(bool)
    p["known_at"] = _utc(p.known_at)
    st = D.load_stints(conn, min_season)
    return build(g, p, st, sigma)


def build(g: pd.DataFrame, p: pd.DataFrame, stints: pd.DataFrame,
          sigma: float = D.SIGMA_DEFAULT) -> Frames:
    p = p.copy()
    p["pts"] = p.pop("points")
    p["reb"] = p.oreb + p.dreb
    p["fg2a"] = p.fga - p.fg3a
    p["fg2m"] = p.fgm - p.fg3m
    p["pra"] = p.pts + p.reb + p.ast
    p["season"] = p.game_id.map(g.season)
    p["tip"] = p.game_id.map(g.tip)
    home = p.game_id.map(g.home)
    p["is_home"] = p.team_id == home
    p["opp_id"] = np.where(p.is_home, p.game_id.map(g.away), home)
    p["played"] = p.minutes > 0
    # team-games from the box
    tg = p.groupby(["game_id", "team_id"]).agg(
        known_at=("known_at", "max"), tip=("tip", "first"), season=("season", "first"),
        is_home=("is_home", "first"), opp_id=("opp_id", "first"),
        team_min=("minutes", "sum"), **{c: (c, "sum") for c in
                                        ("pts", "fga", "fgm", "fg2a", "fg2m", "fg3a", "fg3m",
                                         "fta", "ftm", "oreb", "dreb", "ast", "tov")})  # fmt: skip
    tg = tg.reset_index()
    tg["poss"] = tg.fga - tg.oreb + tg.tov + 0.44 * tg.fta
    # derived facts from stints
    pg, gr = D.derive(stints, sigma)
    gr = gr.set_index("game_id")
    ot = ((tg.team_min / 5 - 48) / 5).round().clip(lower=0)
    tg["game_min"] = 48 + 5 * ot
    opp = tg.set_index(["game_id", "team_id"])
    ok = pd.MultiIndex.from_arrays([tg.game_id, tg.opp_id])
    for c in ("poss", "pts", "fga", "fgm", "fg3a", "fg3m", "fg2a", "fg2m", "fta", "oreb", "dreb",
              "tov"):
        tg[f"o_{c}"] = opp[c].reindex(ok).to_numpy()
    tg["complete"] = tg.game_id.map(gr.complete).fillna(False).astype(bool)
    for d in D.DEFS:
        tg[f"nc_sec_{d}"] = tg.game_id.map(gr[f"nc_sec_{d}"]).fillna(0.0).where(tg.complete,
                                                                                 0.0)  # fmt: skip
    # player-games
    key = pd.MultiIndex.from_arrays([p.game_id, p.team_id])
    tgi = tg.set_index(["game_id", "team_id"])
    p["team_poss"] = tgi.poss.reindex(key).to_numpy()
    p["game_min"] = tgi.game_min.reindex(key).to_numpy()
    p["complete"] = tgi.complete.reindex(key).to_numpy().astype(bool)
    p["poss_on"] = p.team_poss * p.minutes / p.game_min
    pg = pg.assign(player_id=pg.player_id.astype(str))
    m = p[["game_id", "player_id"]].astype({"player_id": str}).merge(
        pg.drop(columns="home_side"), on=["game_id", "player_id"], how="left")  # fmt: skip
    for c in ("sec_on", "last_on", *(f"sec_nc_{d}" for d in D.DEFS)):
        p[c] = m[c].to_numpy()
    for d in D.DEFS:
        nc = tgi[f"nc_sec_{d}"].reindex(key).to_numpy()
        p[f"nc_sec_{d}"] = nc
        comp = p.game_min * 60 - nc
        on_nc = np.where(p.complete, np.nan_to_num(p[f"sec_nc_{d}"]), 0.0)
        on = np.where(p.complete & p.sec_on.notna(), p.sec_on, p.minutes * 60)
        p[f"c_{d}"] = np.clip((on - on_nc) / comp, 0, 1)  # competitive share
        p[f"g_{d}"] = np.where(nc >= 120, on_nc / np.maximum(nc, 1), np.nan)  # garbage share
    p["ot"] = p.game_min > 48
    p["fouls5"] = p.pf >= 5
    return g, p.sort_values(["tip", "game_id", "team_id", "player_id"]).reset_index(drop=True), tg


def flags(p: pd.DataFrame, d: str, baseline: pd.Series | None = None) -> pd.DataFrame:
    """B4 outlier flags per player-game for definition `d`. `baseline` is the player's minutes
    projection before the game (for the early-exit rule); without it his previous 10-game
    average is used."""
    out = pd.DataFrame(index=p.index)
    blow = (p[f"nc_sec_{d}"] / 60) >= BLOWOUT_MIN
    out["blowout"] = blow & p.played
    out["ot"] = p.ot & p.played
    out["fouls"] = p.fouls5 & p.played
    if baseline is None:
        pl = p[p.played].sort_values("tip")
        baseline = pl.groupby("player_id").minutes.transform(
            lambda s: s.shift(1).rolling(10, 3).mean()).reindex(p.index)  # fmt: skip
    left_early = p.complete & (p.last_on < 2880 - 720) & p.last_on.notna()
    out["exit"] = p.played & left_early & ~blow & (p.minutes < 0.6 * baseline.fillna(np.inf))
    return out
