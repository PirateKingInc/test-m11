"""The live NBA model: the frozen fit (config/fits/, never refitted hourly) predicting games in
the next 2 days at the point each has reached, stored change-only in model_predictions.

A game is predicted `early` before its morning point, then at `morning` and `pretip` (MODEL.md).
If a point passed with no stored prediction (the runner was down), one is made as of that
point from what was known then, so every game has both points to grade.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, desc, insert, select

from betdata.config import PROJECT_ROOT
from betdata.core.matching import season_for
from betdata.db.schema import model_predictions
from betdata.moneyline import no_vig
from betdata.nba.features import (
    GROUPS,
    play_prob,
    schedule_table,
    status_key,
    streak_for,
    team_status,
)
from betdata.nba.pipeline import Fit
from betdata.nba.points import MORNING, POINTS, PRETIP, morning_time, point_time, pretip_time
from betdata.nba.state import League, made_by_game
from betdata.nfl.features.state import advanced
from betdata.nfl.model import Ridge
from betdata.nfl.store import AsOf, Store
from betdata.quotes import current_quotes

log = logging.getLogger(__name__)
DESIGN_PATH = PROJECT_ROOT / "config" / "nba_model.json"
MODEL = "nba"
DAYS = 2
LIVE_SEASONS = 3  # the live store holds the current season and the previous ones up to this
EARLY = "early"
LABELS = {  # factor labels for the page
    "m_base_elo": "Elo rating", "m_base_home": "home court",
    "m_avail_new": "players newly out", "m_avail_new_pm": "players newly out (+/-)",
    "m_avail_long": "long absences", "m_avail_back": "players returning",
    "m_avail_n_out": "rotation players out", "m_avail_star": "stars out",
    "m_avail_multi": "several players out", "m_avail_q": "questionable players",
    "m_str_net": "team rating", "m_str_fast": "recent form", "m_str_lineup_g": "expected lineup",
    "m_str_lineup_pm": "expected lineup (+/-)", "m_rest_diff": "rest days",
    "m_rest_b2b": "back-to-back", "m_rest_b2b_first": "back-to-back next day",
    "m_rest_in4": "3 games in 4 nights", "m_rest_in6": "4 games in 6 nights",
    "m_rest_km": "travel", "m_rest_tz": "time zones crossed", "m_rest_alt": "altitude",
    "m_rest_trip": "road trip length", "m_rest_first_home": "first game home after a trip",
    "m_hca_est": "home court (this season)", "m_hca_bubble": "bubble",
    "m_hca_crowds": "limited crowds", "m_ctx_prior": "last season", "m_ctx_kept": "roster kept",
    "m_ctx_tank": "tanking", "m_ctx_rest": "resting starters", "m_ctx_post": "playoffs",
    "m_mkt_line": "market line", "m_mkt_move": "line movement",
}  # fmt: skip


@dataclass
class Design:
    groups: list[str]
    lam_m: float
    lam_t: float
    spread_threshold: float
    ml_threshold: float
    blend_weight: float  # model weight in the model+market blend (validation)
    fit: str  # path of the frozen fit, relative to the project
    ml_final_test_roi: float | None = None
    calibration: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = DESIGN_PATH) -> Design:
        return cls(**json.loads(path.read_text()))


def load_fit(design: Design) -> tuple[dict[str, Fit], dict, str]:
    raw = json.loads((PROJECT_ROOT / design.fit).read_text())
    fits = {p: Fit(p, Ridge.from_dict(f["margin"]), Ridge.from_dict(f["total"]), f["sigma"])
            for p, f in raw["fits"].items()}  # fmt: skip
    version = "nba:" + hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:10]
    return fits, raw.get("play_prob", {}), version


def point_now(tip: datetime, now: datetime) -> str:
    if now >= pretip_time(tip):
        return PRETIP
    return MORNING if now >= morning_time(tip) else EARLY


@dataclass
class Pick:
    game_id: int
    point: str
    margin: float
    total: float
    win_prob: float
    factors: list
    missing: dict
    notes: list
    features: dict
    market_spread: float | None = None
    market_total: float | None = None
    ml_home: int | None = None
    ml_away: int | None = None
    blend_margin: float | None = None


def _players(a: AsOf, g, team: int) -> list[str]:
    """Players out or uncertain, with the model's play probability."""
    st = advanced(a, "league", League)
    status, reported = team_status(a, g, team)
    out = []
    for pid, p in sorted(st.rotation(team), key=lambda x: -x[1].min_play):
        s = status.get(pid)
        streak = streak_for(st, g, p)
        prob = play_prob(a, status_key(s, streak, reported))
        if prob < 0.9:
            name = a.store.names.get(pid, pid)
            tag = s or f"missed last {streak}"
            out.append(f"{name} ({tag}, {100 * prob:.0f}% to play)")
    # Listed on the report but not (yet) in the team's rotation: new arrivals and players
    # without minutes. Shown, not counted by the model (it values players by their minutes).
    rotation = {pid for pid, _ in st.rotation(team)}
    for r in reversed(advanced(a, "inj", made_by_game("injuries")).rows.get(g.game_id, [])):
        if r.team_id == team and r.player_id not in rotation and r.status in (
                "Out", "Doubtful", "Questionable"):  # fmt: skip
            label = f"{r.player_name} ({r.status}, {100 * play_prob(a, r.status):.0f}% to play)"
            if label not in out:
                out.append(label)
    seen, unique = set(), []
    for x in out:  # a player's later report row replaces his earlier one
        name = x.split(" (")[0]
        if name not in seen:
            seen.add(name)
            unique.append(x)
    return unique[:8]


def _notes(store: Store, g) -> list[str]:
    s = schedule_table(store)
    out = []
    for team, abbr in ((g.home, g.home_abbr), (g.away, g.away_abbr)):
        x = s[(g.game_id, team)]
        if x["b2b"]:
            out.append(f"{abbr}: 2nd night of a back-to-back")
        elif 3 <= x["rest"] < 5:  # 5 = capped; also the first game of a season
            out.append(f"{abbr}: {x['rest']:.0f} days' rest")
        if x["in4"] and not x["b2b"]:
            out.append(f"{abbr}: 3rd game in 4 nights")
        if x["alt"]:
            out.append(f"{abbr}: playing at altitude")
        if x["trip"] >= 4:
            out.append(f"{abbr}: game {x['trip']:.0f} of a road trip")
    return out


def predict_game(store: Store, design: Design, fits: dict[str, Fit], g, point: str,
                 t: datetime) -> Pick:  # fmt: skip
    groups = [GROUPS["base"], *(GROUPS[n] for n in design.groups)]
    a = AsOf(store, t)
    x: dict[str, float] = {}
    for grp in groups:
        x.update(grp.compute(a, g, point))
    fit = fits[MORNING if point == EARLY else point]
    margin, total = fit.predict(x)
    contrib = fit.margin.contributions(x)
    factors = sorted(((LABELS.get(k, k), round(v, 2)) for k, v in contrib.items()
                      if abs(v) >= 0.05), key=lambda kv: -abs(kv[1]))  # fmt: skip
    wp = 0.5 * (1 + math.erf(margin / (fit.sigma * math.sqrt(2))))
    return Pick(int(g.game_id), point, round(margin, 2), round(total, 1), round(wp, 4),
                factors[:8], {"home": _players(a, g, g.home), "away": _players(a, g, g.away)},
                _notes(store, g), {k: round(float(v), 4) for k, v in x.items()})  # fmt: skip


def attach_market(conn, picks: list[Pick], t: datetime, design: Design) -> None:
    quotes = current_quotes(conn, [p.game_id for p in picks], t)
    for p in picks:
        q = quotes.get(p.game_id, {})
        if q.get("spread_home"):
            p.market_spread = q["spread_home"].line
            w = design.blend_weight
            p.blend_margin = round(w * p.margin + (1 - w) * -p.market_spread, 2)
        if q.get("total"):
            p.market_total = q["total"].line
        if q.get("ml_home") and q.get("ml_away") and no_vig(q["ml_home"].price,
                                                            q["ml_away"].price):  # fmt: skip
            p.ml_home, p.ml_away = q["ml_home"].price, q["ml_away"].price


def store_picks(conn, picks: list[Pick], version: str, t: datetime) -> int:
    mp, n = model_predictions, 0
    for p in picks:
        last = conn.execute(
            select(mp.c.pred_home_margin, mp.c.pred_total, mp.c.market_spread_home,
                   mp.c.market_ml_home, mp.c.market_ml_away, mp.c.factors)
            .where(mp.c.model_version == version, mp.c.game_id == p.game_id,
                   mp.c.prediction_point == p.point)
            .order_by(desc(mp.c.predicted_at)).limit(1)).first()  # fmt: skip
        factors = json.loads(json.dumps({"margin": p.factors, "missing": p.missing,
                                         "notes": p.notes}))  # as stored (tuples -> lists)
        now_vals = (p.margin, p.total, p.market_spread, p.ml_home, p.ml_away)
        if last is not None and tuple(last[:5]) == now_vals and (last[5] or {}) == factors:
            continue
        conn.execute(insert(mp).values(
            model=MODEL, model_version=version, game_id=p.game_id, predicted_at=t,
            pred_home_margin=p.margin, pred_total=p.total, pred_home_win_prob=p.win_prob,
            market_spread_home=p.market_spread, market_total=p.market_total,
            market_source="poll" if p.market_spread is not None else None,
            prediction_point=p.point, blend_home_margin=p.blend_margin, features=p.features,
            factors=factors, params={"design": "config/nba_model.json"},
            market_ml_home=p.ml_home, market_ml_away=p.ml_away))  # fmt: skip
        n += 1
    return n


def loaded_store(engine: Engine, now: datetime, play: dict) -> Store:
    from betdata.nba.data import load_store

    with engine.connect() as conn:
        store = load_store(conn, min_season=season_for("nba", now) - LIVE_SEASONS)
    return store.with_params(play_prob=play)


def run_predict(engine: Engine, now: datetime, days: int = DAYS) -> list[Pick]:
    design = Design.load()
    fits, play, version = load_fit(design)
    store = loaded_store(engine, now, play)
    g = store.games
    upcoming = [i for i in g.index if now < g.at[i, "tip"] <= now + timedelta(days=days)
                and g.at[i, "status"] == "scheduled"]  # fmt: skip
    mp = model_predictions
    with engine.begin() as conn:
        have = {(r.game_id, r.prediction_point) for r in conn.execute(
            select(mp.c.game_id, mp.c.prediction_point).where(
                mp.c.model_version == version, mp.c.game_id.in_(upcoming)))}  # fmt: skip
        for gid in upcoming:  # points that passed with no stored prediction
            for point in POINTS:
                t = point_time(point, g.at[gid, "tip"])
                if t <= now and (gid, point) not in have and point_now(g.at[gid, "tip"],
                                                                       now) != point:
                    back = [predict_game(store, design, fits, g.loc[gid], point, t)]
                    attach_market(conn, back, t, design)
                    store_picks(conn, back, version, t)
        picks = [predict_game(store, design, fits, g.loc[gid],
                              point_now(g.at[gid, "tip"], now), now) for gid in upcoming]
        attach_market(conn, picks, now, design)
        store_picks(conn, picks, version, now)
    return picks
