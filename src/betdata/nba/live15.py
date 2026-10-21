"""The live nba15 side version (M15): the frozen nba15 fit (config/fits/, never refitted
hourly) predicting the next 2 days beside the frozen M14 version, stored as model "nba15" and
graded live. The page shows it only if config/nba15_model.json says `page: true` (M15 Part A
switch rule); otherwise it runs in the background.

Inputs beyond M14's: the load-management table (from the fit file) and this week's RAPM
snapshot, fitted on the stints known before the latest Monday (no hourly refit)."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import Engine, desc, insert, select

from betdata.config import PROJECT_ROOT
from betdata.core.matching import season_for
from betdata.db.schema import model_predictions
from betdata.nba.features import GROUPS
from betdata.nba.features15 import GROUPS15
from betdata.nba.live import (
    DAYS,
    EARLY,
    LABELS,
    Pick,
    _notes,
    _players,
    attach_market,
    loaded_store,
    point_now,
)
from betdata.nba.pipeline import Fit
from betdata.nba.points import MORNING, POINTS, point_time
from betdata.nfl.model import Ridge
from betdata.nfl.store import AsOf

log = logging.getLogger(__name__)
MODEL = "nba15"
DESIGN_PATH = PROJECT_ROOT / "config" / "nba15_model.json"
LABELS15 = {"m_rapm_net": "lineup RAPM", "m_luck3_net": "3-point shooting luck",
            "m_load_exp": "load management", "m_style_reb": "rebounding matchup",
            "m_style_3pa": "three-point volume matchup", "m_style_tov": "turnover matchup",
            "m_style_ft": "free-throw matchup", "m_integ_new": "new players",
            "m_integ_joined": "recent arrivals", "m_integ_lost": "recent departures"}  # fmt: skip


@dataclass
class Design15:
    groups: list
    lam_m: float
    lam_t: float
    fit: str
    page: bool = False

    @classmethod
    def load(cls) -> Design15 | None:
        if not DESIGN_PATH.exists():
            return None
        d = json.loads(DESIGN_PATH.read_text())
        if not d.get("fit"):
            return None
        return cls(d["groups"], d["lam_m"], d["lam_t"], d["fit"], bool(d.get("page")))


def load_fit15(d: Design15) -> tuple[dict, dict, dict, str]:
    raw = json.loads((PROJECT_ROOT / d.fit).read_text())
    fits = {p: Fit(p, Ridge.from_dict(f["margin"]), Ridge.from_dict(f["total"]), f["sigma"])
            for p, f in raw["fits"].items()}  # fmt: skip
    version = MODEL + ":" + hashlib.sha256(json.dumps(raw, sort_keys=True).encode()
                                           ).hexdigest()[:10]  # fmt: skip
    return fits, raw.get("play_prob", {}), raw.get("load_sit", {}), version


def version() -> str | None:
    d = Design15.load()
    return load_fit15(d)[3] if d else None


def rapm_snapshot(engine: Engine, now: datetime) -> dict:
    """{latest Monday 12:00 UTC: snapshot} from stints known before it."""
    from betdata.nba.rapm import load_stints, snapshots

    t = pd.Timestamp(now).tz_convert("UTC").normalize()
    monday = t - pd.Timedelta(days=t.weekday()) + pd.Timedelta(hours=12)
    if monday > pd.Timestamp(now):
        monday -= pd.Timedelta(days=7)
    with engine.connect() as conn:
        st = load_stints(conn, season_for("nba", now) - 1)
    return snapshots(st, [monday]) if len(st) else {}


def _groups(d: Design15) -> list:
    return [GROUPS["base"], *((GROUPS15 | GROUPS)[n] for n in d.groups)]


def predict_game(store, d: Design15, fits, g, point: str, t) -> Pick:
    a = AsOf(store, t)
    x: dict[str, float] = {}
    for grp in _groups(d):
        x.update(grp.compute(a, g, point))
    fit = fits[MORNING if point == EARLY else point]
    margin, total = fit.predict(x)
    labels = LABELS | LABELS15
    factors = sorted(((labels.get(k, k), round(v, 2)) for k, v in fit.margin.contributions(
        x).items() if abs(v) >= 0.05), key=lambda kv: -abs(kv[1]))  # fmt: skip
    wp = 0.5 * (1 + math.erf(margin / (fit.sigma * math.sqrt(2))))
    return Pick(int(g.game_id), point, round(margin, 2), round(total, 1), round(wp, 4),
                factors[:8], {"home": _players(a, g, g.home), "away": _players(a, g, g.away)},
                _notes(store, g), {k: round(float(v), 4) for k, v in x.items()})  # fmt: skip


def store_picks(conn, picks: list[Pick], ver: str, t: datetime) -> int:
    mp, n = model_predictions, 0
    for p in picks:
        last = conn.execute(
            select(mp.c.pred_home_margin, mp.c.pred_total, mp.c.market_spread_home,
                   mp.c.market_ml_home, mp.c.market_ml_away, mp.c.factors)
            .where(mp.c.model_version == ver, mp.c.game_id == p.game_id,
                   mp.c.prediction_point == p.point)
            .order_by(desc(mp.c.predicted_at)).limit(1)).first()  # fmt: skip
        factors = json.loads(json.dumps({"margin": p.factors, "missing": p.missing,
                                         "notes": p.notes}))  # fmt: skip
        vals = (p.margin, p.total, p.market_spread, p.ml_home, p.ml_away)
        if last is not None and tuple(last[:5]) == vals and (last[5] or {}) == factors:
            continue
        conn.execute(insert(mp).values(
            model=MODEL, model_version=ver, game_id=p.game_id, predicted_at=t,
            pred_home_margin=p.margin, pred_total=p.total, pred_home_win_prob=p.win_prob,
            market_spread_home=p.market_spread, market_total=p.market_total,
            market_source="poll" if p.market_spread is not None else None,
            prediction_point=p.point, blend_home_margin=p.blend_margin, features=p.features,
            factors=factors, params={"design": "config/nba15_model.json"},
            market_ml_home=p.ml_home, market_ml_away=p.ml_away))  # fmt: skip
        n += 1
    return n


def run_predict15(engine: Engine, now: datetime, days: int = DAYS) -> list[Pick]:
    from betdata.nba.live import Design

    d = Design15.load()
    if d is None:
        return []
    fits, play, load, ver = load_fit15(d)
    store = loaded_store(engine, now, play).with_params(load_sit=load,
                                                       rapm=rapm_snapshot(engine, now))
    g = store.games
    upcoming = [i for i in g.index if now < g.at[i, "tip"] <= now + timedelta(days=days)
                and g.at[i, "status"] == "scheduled"]  # fmt: skip
    m14 = Design.load()
    mp = model_predictions
    with engine.begin() as conn:
        have = {(r.game_id, r.prediction_point) for r in conn.execute(
            select(mp.c.game_id, mp.c.prediction_point).where(
                mp.c.model_version == ver, mp.c.game_id.in_(upcoming)))}  # fmt: skip
        for gid in upcoming:
            for point in POINTS:
                t = point_time(point, g.at[gid, "tip"])
                if t <= now and (gid, point) not in have and point_now(g.at[gid, "tip"],
                                                                       now) != point:
                    back = [predict_game(store, d, fits, g.loc[gid], point, t)]
                    attach_market(conn, back, t, m14)
                    store_picks(conn, back, ver, t)
        picks = [predict_game(store, d, fits, g.loc[gid], point_now(g.at[gid, "tip"], now),
                              now) for gid in upcoming]  # fmt: skip
        attach_market(conn, picks, now, m14)
        store_picks(conn, picks, ver, now)
    return picks
