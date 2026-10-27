"""Live NBA prop predictions (M15): every player with a stored prop line in a game of the next
2 days, from the frozen prop fit (config/fits/, never refitted hourly), at the point the game
has reached, stored change-only in prop_predictions."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import Connection, Engine, desc, insert, select

from betdata.config import PROJECT_ROOT
from betdata.core.matching import season_for
from betdata.db.schema import prop_predictions
from betdata.moneyline import no_vig
from betdata.nba.live import DAYS, point_now
from betdata.nba.live import Design as SideDesign
from betdata.nba.live import load_fit as load_side_fit
from betdata.nba.props import model as M
from betdata.nba.props.grade import DESIGN_PATH, MODEL
from betdata.nba.props.panel import MARKETS, Panel
from betdata.nba.props.side import side_live
from betdata.props.lines import latest_lines

log = logging.getLogger(__name__)
UNIT = {"points": "pts", "rebounds": "reb", "assists": "ast", "threes": "3PM", "pra": "PRA"}


@dataclass
class PropDesign:
    fit: str  # the frozen prop fit, relative to the project
    thresholds: dict[str, float | None] = field(default_factory=dict)  # backtested markets
    test_roi: dict[str, float | None] = field(default_factory=dict)  # final-test ROI per market
    research_only: list[str] = field(default_factory=list)  # markets without a backtest
    # 95% range of the final-test ROI; a market whose range includes zero is "info only"
    test_roi_95: dict[str, list[float]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> PropDesign:
        return cls(**json.loads(DESIGN_PATH.read_text()))

    def flag_allowed(self, market: str) -> bool:
        roi = self.test_roi.get(market)
        return (market not in self.research_only and roi is not None and roi > 0
                and not self.info_only(market))  # fmt: skip

    def info_only(self, market: str) -> bool:
        """Backtested with a positive ROI whose 95% range includes zero: shown, never flagged
        (decision 2026-10-27, MODEL.md "Prop flags after the M16 audit")."""
        lo_hi = self.test_roi_95.get(market)
        roi = self.test_roi.get(market)
        return (market not in self.research_only and roi is not None and roi > 0
                and lo_hi is not None and lo_hi[0] <= 0)  # fmt: skip


def load_model(design: PropDesign) -> tuple[M.PropModel, str]:
    raw = json.loads((PROJECT_ROOT / design.fit).read_text())
    version = MODEL + ":" + hashlib.sha256(json.dumps(raw, sort_keys=True).encode()
                                           ).hexdigest()[:10]  # fmt: skip
    return M.PropModel.from_dict(raw["model"]), version


def p_over_no_push(mod: M.PropModel, market: str, mu, line) -> np.ndarray:
    """P(over | no push): comparable with the no-vig over probability of a two-way market."""
    p = mod.p_over(market, mu, line)
    push = mod.disp[market].p_push(mu, line)
    return np.where(push < 1, p / np.maximum(1 - push, 1e-9), p)


def factors(mod: M.PropModel, f: pd.DataFrame, market: str, m_hat: np.ndarray,
            out_names: list[str]) -> list[list]:  # fmt: skip
    """The two largest pushes on this projection, in the stat's units (vs the training
    average): teammates out, opponent, game total, rest, blowout risk, own status."""
    s, unit = MARKETS[market], UNIT[market]
    terms = M.stat_terms(f, m_hat, market)
    c = mod.stats[market].contributions(terms.iloc[0].to_dict())
    mc = mod.minutes.contributions(f.iloc[0][list(mod.minutes.names)].fillna(0).to_dict())
    rate = float(f.iloc[0][f"r30_{s}"] or 0.0)

    def mins(*k):
        return sum(mc.get(x, 0.0) for x in k) * rate

    who = ", ".join(out_names[:2])
    cands = [
        (f"{who} out" if who else "teammates out", c.get("redis", 0.0)
         + mins("mis_min", "mis_start_min")),
        ("opponent defence", c.get("opp", 0.0)),
        ("game total", c.get("env", 0.0)),
        ("rest / back-to-back", mins("b2b", "rest")),
        ("blowout risk", mins("abs_margin", "abs_margin_start")),
        ("own injury status", mins("own_q", "own_prob")),
        ("returning from absence", mins("ret_flag", "since_return_c")),
    ]  # fmt: skip
    cands = [(k, round(v, 2)) for k, v in cands if abs(v) >= 0.05]
    cands.sort(key=lambda kv: -abs(kv[1]))
    return [[k, v, unit] for k, v in cands[:2]]


def predict(conn: Connection, now: datetime, panel: Panel | None = None,
            design: PropDesign | None = None) -> list[dict]:  # fmt: skip
    design = design or PropDesign.load()
    mod, version = load_model(design)
    panel = panel or Panel.load(conn, season_for("nba", now) - 1)
    g = panel.g
    upcoming = g[(g.status == "scheduled") & (g.tip > now) & (g.tip <= now + timedelta(days=DAYS))]
    lines = latest_lines(conn, upcoming.index, now)
    lines = lines[lines.market.isin(list(MARKETS))]
    if lines.empty:
        return []
    st = panel.states.sort_values("known_at")
    st = st[st.known_at <= now].groupby("player_id").last()
    lines = lines[lines.player_id.isin(st.index)].copy()
    lines["team_id"] = lines.player_id.map(st.team_id)
    home, away = lines.game_id.map(g.home), lines.game_id.map(g.away)
    lines = lines[(lines.team_id == home) | (lines.team_id == away)]
    if lines.empty:
        return []
    lines["point"] = [point_now(g.at[gid, "tip"], now) for gid in lines.game_id]
    lines["t"] = pd.Timestamp(now)
    side = side_live(conn, upcoming.index, now)
    side = side.assign(point=side.game_id.map(dict(zip(lines.game_id, lines.point, strict=False))))
    play = load_side_fit(SideDesign.load())[1]
    tg = lines[["game_id", "team_id", "player_id", "point", "t"]].drop_duplicates()
    f = M.prepare(panel.features(tg, side, play))
    f = f[f.has_state]
    f = f.set_index(["game_id", "player_id"])
    out_by_team = _out_names(panel, tg, now)
    picks = []
    for r in lines.itertuples():
        key = (r.game_id, r.player_id)
        if key not in f.index:
            continue
        row = f.loc[[key]].reset_index()
        m_hat = mod.predict_minutes(row)
        mu = float(mod.predict(row, r.market, m_hat)[0])
        p = float(p_over_no_push(mod, r.market, [mu], [r.line])[0])
        nv = no_vig(r.over_price, r.under_price) if pd.notna(r.over_price) and pd.notna(
            r.under_price) else None  # fmt: skip
        edge = round(100 * (p - nv[0]), 1) if nv else None
        disp = mod.disp[r.market]
        picks.append(dict(
            game_id=int(r.game_id), player_id=str(r.player_id),
            player_name=r.player_name or str(r.player_id), team_id=int(r.team_id),
            market=r.market, prediction_point=r.point, mean=round(mu, 2),
            line=float(r.line), over_price=_int(r.over_price), under_price=_int(r.under_price),
            book_id=int(r.book_id), line_fetched_at=r.fetched_at.to_pydatetime(),
            p_over=round(p, 4), edge=edge,
            dist={**disp.to_dict(), "mean": round(mu, 3), "minutes": round(float(m_hat[0]), 1)},
            factors=factors(mod, row, r.market, m_hat, out_by_team.get(
                (r.game_id, r.team_id, r.player_id), []))))  # fmt: skip
    return [dict(p, model=MODEL, model_version=version) for p in picks]


def _int(x):
    return None if x is None or pd.isna(x) else int(x)


def _out_names(panel: Panel, tg: pd.DataFrame, now) -> dict:
    """Per target: teammates listed Out or Doubtful by now, biggest scorers first."""
    inj = panel.inj[(panel.inj.known_at <= now) & panel.inj.game_id.isin(tg.game_id)]
    inj = inj.sort_values("known_at").drop_duplicates(["game_id", "player_id"], keep="last")
    inj = inj[inj.status.isin(["Out", "Doubtful"])]
    st = panel.states.sort_values("known_at").groupby("player_id").last()
    out = {}
    for r in tg.itertuples():
        mates = inj[(inj.game_id == r.game_id) & (inj.team_id == r.team_id)
                    & (inj.player_id != r.player_id)]  # fmt: skip
        mates = mates.assign(ppg=mates.player_id.map(st.g10_pts).fillna(0))
        mates = mates[mates.ppg >= 8].sort_values("ppg", ascending=False)
        out[(r.game_id, r.team_id, r.player_id)] = [
            _short(n) for n in mates.player_name.tolist()]  # fmt: skip
    return out


def _short(name: str) -> str:
    """'Embiid, Joel' -> 'Embiid'."""
    return str(name).split(",")[0].strip() if "," in str(name) else str(name).split()[-1]


def store(conn: Connection, picks: list[dict], now: datetime) -> int:
    t, n = prop_predictions, 0
    for p in picks:
        last = conn.execute(
            select(t.c.mean, t.c.line, t.c.over_price, t.c.under_price, t.c.factors)
            .where(t.c.model_version == p["model_version"], t.c.game_id == p["game_id"],
                   t.c.player_id == p["player_id"], t.c.market == p["market"],
                   t.c.prediction_point == p["prediction_point"])
            .order_by(desc(t.c.predicted_at)).limit(1)).first()  # fmt: skip
        fac = json.loads(json.dumps(p["factors"]))
        if last is not None and (last.mean, last.line, last.over_price, last.under_price,
                                 last.factors) == (p["mean"], p["line"], p["over_price"],
                                                   p["under_price"], fac):  # fmt: skip
            continue
        conn.execute(insert(t).values(**{**p, "factors": fac}, predicted_at=now))
        n += 1
    return n


def run(engine: Engine, now: datetime) -> int:
    if not DESIGN_PATH.exists():
        return 0
    with engine.begin() as conn:
        picks = predict(conn, now)
        return store(conn, picks, now)
