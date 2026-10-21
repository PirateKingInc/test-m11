"""Live NFL prop predictions (M15): every player with a stored prop line in a scheduled game of
the next 8 days, from the frozen prop fit (config/fits/, never refitted hourly), at the M12
point the game has reached (early / final_report / prekick), stored change-only in
prop_predictions as model "nflp15", with the two biggest factors in the stat's units.

Prop lines carry ESPN athlete ids; they are mapped to gsis ids by nflverse's ESPN-based depth
charts (2025+), else by a unique name (nfl/props/ids.py). Inputs are as in history: M12's store
(snaps, rosters, injury reports with M12's play probabilities, stored in the fit), nflverse
weekly stats and play-by-play of this and last season, and the side model's latest stored
prediction with its weather features (nfl/props/side.py). Route participation has no 2026 file:
snap share stands in.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import Connection, Engine

from betdata.config import PROJECT_ROOT
from betdata.core.matching import season_for
from betdata.moneyline import no_vig
from betdata.nba.props.live import PropDesign
from betdata.nba.props.live import store as store_picks
from betdata.nfl.props import model as M
from betdata.nfl.props.grade import DESIGN_PATH, MODEL
from betdata.props.lines import latest_lines

DAYS = 8
UNIT = {"pass_yds": "yds", "rush_yds": "yds", "rec_yds": "yds", "receptions": "rec",
        "anytime_td": "TD"}  # fmt: skip


def load_design() -> PropDesign:
    return PropDesign(**json.loads(DESIGN_PATH.read_text()))


def load_model(design: PropDesign) -> tuple[M.PropModel, str, dict]:
    raw = json.loads((PROJECT_ROOT / design.fit).read_text())
    version = MODEL + ":" + hashlib.sha256(json.dumps(raw, sort_keys=True).encode()
                                           ).hexdigest()[:10]  # fmt: skip
    return M.PropModel.from_dict(raw["model"]), version, raw.get("play_params", {})


def p_over_no_push(mod: M.PropModel, market: str, mu, line) -> np.ndarray:
    """P(over | no push): comparable with the no-vig over probability of a two-way market."""
    p = mod.p_over(market, mu, line)
    push = mod.p_push(market, mu, line)
    return np.where(push < 1, p / np.maximum(1 - push, 1e-9), p)


def factors(mod: M.PropModel, row: pd.DataFrame, market: str, vol: dict) -> list[list]:
    """The two largest pushes on this projection in the stat's units: wind, teammates out,
    opponent, man/zone matchup, game script (side margin and total through team volume), own
    injury status."""
    return factors_many(mod, row, market, vol)[0]


def factors_many(mod: M.PropModel, rows: pd.DataFrame, market: str, vol: dict,
                 mu: np.ndarray | None = None) -> list[list[list]]:  # fmt: skip
    """`factors` for every row of one market at once (terms and volumes computed once)."""
    t = mod.terms(rows, market, vol)
    mu = mod.predict(rows, market, vol) if mu is None else mu
    v = {"pass_yds": "att", "rush_yds": "car"}.get(market, "tgt")
    tx = M.team_x(rows, v)
    small = 0.5 if UNIT[market] == "yds" else 0.03
    out = []
    for i in range(len(rows)):
        c = mod.stats[market].contributions(t.iloc[i].to_dict())
        vc = mod.team[v].contributions(tx.iloc[i].to_dict())
        per_vol = float(mu[i]) / max(float(vol[v][i]), 1.0) if market != "anytime_td" else 0.0
        r = rows.iloc[i]
        names = [str(n).split()[-1] for n in (r.out_names if isinstance(r.out_names, list)
                                              else [])]  # fmt: skip
        wind = float(r.wind or 0.0)
        cands = [
            (f"wind {wind + 10:.0f} km/h" if wind > 0 else "light wind",
             c.get("wind", 0.0) + vc.get("wind", 0.0) * per_vol),
            (f"{', '.join(names)} out" if names else "teammates out",
             c.get("redis", 0.0) + c.get("mates", 0.0)),
            ("opponent defence", c.get("opp", 0.0)),
            ("man/zone matchup", c.get("mz", 0.0)),
            ("game script", (vc.get("side_margin", 0.0) + vc.get("side_total", 0.0)) * per_vol
             + c.get("rz_imp", 0.0)),
            ("own injury status", c.get("own_q", 0.0)),
        ]  # fmt: skip
        cands = [(k, round(x, 1 if UNIT[market] == "yds" else 2)) for k, x in cands
                 if abs(x) >= small]  # fmt: skip
        cands.sort(key=lambda kv: -abs(kv[1]))
        out.append([[k, x, UNIT[market]] for k, x in cands[:2]])
    return out


def predict(conn: Connection, now: datetime, design: PropDesign | None = None,
            store=None) -> list[dict]:  # fmt: skip
    from betdata.nfl.data import load_store
    from betdata.nfl.predict import point_now
    from betdata.nfl.props.ids import espn_to_gsis
    from betdata.nfl.props.panel import Panel, availability, healthy_play, teammate_context
    from betdata.nfl.props.side import side_live
    from betdata.nfl.workflow import data_dir

    design = design or load_design()
    mod, version, params = load_model(design)
    season = season_for("nfl", now)
    if store is None:
        store = load_store(conn, first_season=season - 1).with_params(**params)
    g = store.games
    soon = (g.kickoff > now) & (g.kickoff <= now + timedelta(days=DAYS))
    up = g[(g.status == "scheduled") & soon]
    lines = latest_lines(conn, up.index, now)
    lines = lines[lines.market.isin(list(M.MARKETS))].copy()
    if lines.empty:
        return []
    panel = Panel.load(conn, store, (season - 1, season), data_dir(), cached=True)
    lines["gsis"] = espn_to_gsis(lines, data_dir(), (season - 1, season), panel.box)
    st = panel.states[panel.states.known_at <= now].sort_values("known_at")
    st = st.groupby("player_id").last()
    lines = lines[lines.gsis.isin(st.index)].copy()
    lines["team_id"] = lines.gsis.map(st.team_id)
    lines["player_name"] = lines.player_name.fillna(lines.gsis.map(st.name))  # NFL lines: no name
    lines = lines[(lines.team_id == lines.game_id.map(g.home))
                  | (lines.team_id == lines.game_id.map(g.away))]  # fmt: skip
    if lines.empty:
        return []
    lines["point"] = [point_now(g.at[gid, "kickoff"], now) for gid in lines.game_id]
    slots = lines[["game_id", "point"]].drop_duplicates().assign(t=pd.Timestamp(now))
    mates = teammate_context(availability(store, slots), panel.states, slots,
                             healthy_play(store.params))  # fmt: skip
    side = side_live(conn, slots.game_id, now).merge(slots[["game_id", "point"]], on="game_id")
    tg = lines[["game_id", "team_id", "gsis", "point"]].drop_duplicates().rename(
        columns={"gsis": "player_id"}).assign(t=pd.Timestamp(now))  # fmt: skip
    f = panel.features(tg, side, mates)
    f = f[f.has_state.astype(bool)].set_index(["game_id", "player_id"])
    f = f[~f.index.duplicated()]
    lines = lines[[k in f.index for k in zip(lines.game_id, lines.gsis, strict=True)]]
    lines = lines.reset_index(drop=True)
    rows = f.loc[list(zip(lines.game_id, lines.gsis, strict=True))].reset_index()
    picks = []
    for market, idx in lines.groupby("market").indices.items():
        # One market's lines at once: volumes, means and factors are vectorised.
        sub, ln = rows.iloc[idx].reset_index(drop=True), lines.iloc[idx]
        vol = mod.volumes(sub)
        mu = mod.predict(sub, market, vol)
        line = ln.line.to_numpy(dtype=float)
        if market == "anytime_td":
            line = np.where(np.isnan(line), 0.5, line)
        p = p_over_no_push(mod, market, mu, line)
        fac = factors_many(mod, sub, market, vol, mu)
        dist = (mod.dists[market].to_dict() if market in mod.dists
                else {"family": "poisson"})  # fmt: skip
        for i, r in enumerate(ln.itertuples()):
            nv = no_vig(r.over_price, r.under_price) if pd.notna(r.over_price) and pd.notna(
                r.under_price) else None  # fmt: skip
            picks.append(dict(
                game_id=int(r.game_id), player_id=str(r.player_id),
                player_name=r.player_name or str(r.player_id), team_id=int(r.team_id),
                market=market, prediction_point=r.point, mean=round(float(mu[i]), 2),
                line=None if pd.isna(r.line) else float(r.line), over_price=_int(r.over_price),
                under_price=_int(r.under_price), book_id=int(r.book_id),
                line_fetched_at=r.fetched_at.to_pydatetime(), p_over=round(float(p[i]), 4),
                edge=round(100 * (float(p[i]) - nv[0]), 1) if nv else None,
                dist={**dist, "mean": round(float(mu[i]), 3), "gsis_id": r.gsis},
                factors=fac[i]))  # fmt: skip
    return [dict(p, model=MODEL, model_version=version) for p in picks]


def _int(x):
    return None if x is None or pd.isna(x) else int(x)


def run(engine: Engine, now: datetime) -> int:
    if not DESIGN_PATH.exists():
        return 0
    with engine.begin() as conn:
        return store_picks(conn, predict(conn, now), now)
