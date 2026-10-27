"""M17 Part E props: the engine's player outputs (`nflpe17`) against nflp15 and the line.

  accuracy  MAE of the engine median and of nflp15's mean on the same player-games: nflp15's
            populations (who its markets are scored on), at the same prediction point. For
            validation (2020-2022) nflp15 is fitted on 2013-2019, for 2023-2025 on 2013-2022,
            as in its own selection and final test. Anytime TD: Brier of P(at least one).
  lines     ESPN BET 2024-2025: Brier of P(over) at the closing line for the engine, nflp15
            and the no-vig line, and the engine's betting record by market (thresholds chosen
            on 2024, scored on 2025).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MARKET_STAT = {"pass_yds": ("pass_yards", "pass_yds"), "rush_yds": ("rush_yards", "rush_yds"),
               "rec_yds": ("rec_yards", "rec_yds"), "receptions": ("receptions", "rec")}


def _nflp15(fit_seasons):
    from betdata.nfl.props import model as M
    from betdata.nfl.props.history import history_features
    from betdata.nfl.props.select import chosen, fit_frozen

    f = history_features()
    return f, fit_frozen(f, fit_seasons, chosen()), M


def accuracy(engine_players: pd.DataFrame, ids: pd.DataFrame, seasons,
             fit_seasons) -> dict:  # fmt: skip
    f, mod, M = _nflp15(fit_seasons)
    f = f[f.season.isin(list(seasons)) & f.has_state.astype(bool)].copy()
    pops = M.populations(f)
    db_to_nv = _db_to_nflverse(ids)
    f["nv_game"] = f.game_id.map(db_to_nv)
    e = engine_players.set_index(["game_id", "point", "player"])
    e = e[~e.index.duplicated()]
    out = {}
    for mk in M.MARKETS:
        d = f[pops[mk]]
        key = pd.MultiIndex.from_arrays([d.nv_game, d.point, d.player_id])
        have = key.isin(e.index)
        d, key = d[have], key[have]
        if d.empty:
            continue
        y = d[f"y_{M.MARKETS[mk]}"].to_numpy(float)
        mu15 = mod.predict(d, mk)
        if mk == "anytime_td":
            pe = e.loc[key, "p_anytime_td"].fillna(0).to_numpy(float)
            p15 = 1 - np.exp(-np.asarray(mu15, float))
            hit = (y >= 1).astype(float)
            out[mk] = {"n": int(len(y)), "coverage": float(have.mean()),
                       "brier_engine": float(np.mean((pe - hit) ** 2)),
                       "brier_nflp15": float(np.mean((p15 - hit) ** 2)),
                       "brier_base_rate": float(np.mean((hit.mean() - hit) ** 2))}
            continue
        stat = MARKET_STAT[mk][0]
        # a player the engine simulated without this stat (e.g. a QB below the top three, a
        # receiver with no designed carries) is predicted 0
        med = e.loc[key, f"{stat}_median"].fillna(0).to_numpy(float)
        mean = e.loc[key, f"{stat}_mean"].fillna(0).to_numpy(float)
        out[mk] = {"n": int(len(y)), "coverage": float(have.mean()),
                   "mae_engine_median": float(np.mean(np.abs(med - y))),
                   "mae_engine_mean": float(np.mean(np.abs(mean - y))),
                   "mae_nflp15": float(np.mean(np.abs(mu15 - y))),
                   "bias_engine_mean": float(np.mean(mean - y)),
                   "bias_nflp15": float(np.mean(mu15 - y))}  # fmt: skip
    return out


def _db_to_nflverse(ids: pd.DataFrame) -> pd.Series:
    from sqlalchemy import select

    from betdata.db.schema import game_source_ids
    from betdata.nfl.cli import _engine

    with _engine().connect() as c:
        rows = c.execute(select(game_source_ids.c.game_id, game_source_ids.c.source_game_id)
                         .where(game_source_ids.c.source == "nflverse")).all()  # fmt: skip
    return pd.Series({g: s for g, s in rows})


def line_frame(engine_players: pd.DataFrame, prop_lines: pd.DataFrame, actual: pd.DataFrame,
               point: str = "prekick") -> pd.DataFrame:  # fmt: skip
    """One row per (game, player, market) with a line: the engine's P(over) at the open and
    close lines, and the actual stat."""
    e = engine_players[engine_players.point == point].set_index(["game_id", "player"])
    e = e[~e.index.duplicated()]
    rows = []
    for r in prop_lines.itertuples(index=False):
        if r.market not in MARKET_STAT or (r.game_id, r.gsis_id) not in e.index:
            continue
        ps = dict(e.loc[(r.game_id, r.gsis_id)].get(f"p_over_{r.market}") or [])
        row = r._asdict()
        for kind in ("open", "close"):
            line = getattr(r, f"{kind}_line")
            row[f"p_over_{kind}"] = ps.get(float(line)) if line == line else None
        stat = MARKET_STAT[r.market][0]
        row["engine_median"] = e.loc[(r.game_id, r.gsis_id)].get(f"{stat}_median")
        rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    act = actual.rename(columns={"rec_yards": "rec_yds", "rush_yards": "rush_yds",
                                 "pass_yards": "pass_yds"})  # fmt: skip
    vals = []
    for r in df.itertuples(index=False):
        k = (r.game_id, r.gsis_id)
        col = r.market if r.market != "receptions" else "receptions"
        vals.append(float(act.loc[k, col]) if k in act.index and col in act.columns else np.nan)
    df["actual"] = vals
    return df
