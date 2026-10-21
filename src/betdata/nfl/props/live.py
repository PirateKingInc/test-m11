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
    t = mod.terms(row, market, vol)
    c = mod.stats[market].contributions(t.iloc[0].to_dict())
    mu = float(mod.predict(row, market, vol)[0])
    v = {"pass_yds": "att", "rush_yds": "car"}.get(market, "tgt")
    tx = M.team_x(row, v).iloc[0].to_dict()
    vc = mod.team[v].contributions(tx)
    per_vol = mu / max(float(vol[v][0]), 1.0) if market != "anytime_td" else 0.0
    r = row.iloc[0]
    names = [str(n).split()[-1] for n in (r.out_names if isinstance(r.out_names, list) else [])]
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
    small = 0.5 if UNIT[market] == "yds" else 0.03
    cands = [(k, round(x, 1 if UNIT[market] == "yds" else 2)) for k, x in cands
             if abs(x) >= small]  # fmt: skip
    cands.sort(key=lambda kv: -abs(kv[1]))
    return [[k, x, UNIT[market]] for k, x in cands[:2]]


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
    picks = []
    for r in lines.itertuples():
        if (r.game_id, r.gsis) not in f.index:
            continue
        row = f.loc[[(r.game_id, r.gsis)]].reset_index()
        vol = mod.volumes(row)
        mu = float(mod.predict(row, r.market, vol)[0])
        line = 0.5 if r.market == "anytime_td" and pd.isna(r.line) else r.line
        p = float(p_over_no_push(mod, r.market, [mu], [line])[0])
        nv = no_vig(r.over_price, r.under_price) if pd.notna(r.over_price) and pd.notna(
            r.under_price) else None  # fmt: skip
        dist = (mod.dists[r.market].to_dict() if r.market in mod.dists
                else {"family": "poisson"})  # fmt: skip
        picks.append(dict(
            game_id=int(r.game_id), player_id=str(r.player_id),
            player_name=r.player_name or str(r.player_id), team_id=int(r.team_id),
            market=r.market, prediction_point=r.point, mean=round(mu, 2),
            line=None if pd.isna(r.line) else float(r.line), over_price=_int(r.over_price),
            under_price=_int(r.under_price), book_id=int(r.book_id),
            line_fetched_at=r.fetched_at.to_pydatetime(), p_over=round(p, 4),
            edge=round(100 * (p - nv[0]), 1) if nv else None,
            dist={**dist, "mean": round(mu, 3), "gsis_id": r.gsis},
            factors=factors(mod, row, r.market, vol)))  # fmt: skip
    return [dict(p, model=MODEL, model_version=version) for p in picks]


def _int(x):
    return None if x is None or pd.isna(x) else int(x)


def run(engine: Engine, now: datetime) -> int:
    if not DESIGN_PATH.exists():
        return 0
    with engine.begin() as conn:
        return store_picks(conn, predict(conn, now), now)
