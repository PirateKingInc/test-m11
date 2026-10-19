"""Live NFL predictions with factor breakdowns (`betdata predict nfl`).

The frozen design (config/nfl_model.json) is refitted on every completed season from 2013,
then each upcoming game is predicted from what is known *now*: with the prekick fit once
inactives are out (75 minutes before kickoff), with the final-report fit before that. Each
prediction is stored with its prediction point, the market line and total seen at that
moment (never a closing line), the model+market blend, the feature snapshot and the factor
breakdown, as a new row whenever any of the numbers moved.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Connection, desc, insert, select

from betdata.db.schema import model_predictions
from betdata.nfl.features.market import market_at
from betdata.nfl.model import win_prob
from betdata.nfl.pipeline import Fit
from betdata.nfl.points import FINAL_REPORT, PREKICK, final_report_time, prekick_time
from betdata.nfl.selection import Design
from betdata.nfl.store import AsOf, Store

EARLY = "early"  # before the final injury report: the final-report fit on what is known now

# Readable names for the features behind a prediction, by name prefix (longest match wins).
LABELS = {
    "base_hfa": "home field",
    "base_elo": "Elo ratings",
    "base_league": "league scoring level",
    "avail_qb_value": "QB quality",
    "avail_qb_change": "QB change",
    "avail_qb_backup_inexp": "inexperienced backup QB",
    "avail_OL": "OL availability",
    "avail_WR": "WR availability",
    "avail_TE": "TE availability",
    "avail_RB": "RB availability",
    "avail_DL": "DL availability",
    "avail_LB": "LB availability",
    "avail_CB": "CB availability",
    "avail_S": "S availability",
    "avail_cluster_ol": "several OL out",
    "avail_cluster_db": "several DBs out",
    "avail_cluster_wr": "several WRs out",
    "avail_returning": "starters returning",
    "avail_new": "new starters",
    "str_pass": "pass offense/defense",
    "str_rush": "run offense/defense",
    "str_success": "success rate",
    "str_early": "early-down EPA",
    "str_explosive": "explosive plays",
    "str_fum_luck": "fumble luck",
    "str_int_off": "INTs thrown",
    "str_int_def": "INTs caught",
    "str_secs": "pace",
    "str_npass": "pass rate",
    "str_new_coach": "new head coach",
    "str_new_qb": "new starting QB",
    "sched_rest": "rest days",
    "sched_short": "short week",
    "sched_bye": "off a bye",
    "sched_travel": "travel",
    "sched_tz": "time zones",
    "sched_body": "body clock",
    "sched_road": "consecutive road games",
    "sched_alt": "altitude",
    "sched_intl": "international game",
    "env_indoor": "roof",
    "env_turf": "turf",
    "env_wind": "wind",
    "env_cold": "cold",
    "env_rain": "rain",
    "ref_pens": "referee penalties",
    "ref_points": "referee scoring",
    "ref_home": "referee home split",
    "coach_go": "4th-down aggressiveness",
    "coach_pass_oe": "early-down pass rate",
    "ctx_div": "division game",
    "mkt_line": "market line",
    "wx_wind": "wind",
    "wx_strong": "strong wind",
    "wx_cold": "cold",
    "wx_hot": "heat",
    "wx_rain": "rain",
}


def label(feature: str) -> str:
    key = feature[2:]  # drop m_ / t_
    best = max((k for k in LABELS if key.startswith(k)), key=len, default=None)
    return LABELS[best] if best else key


def group_factors(contrib: dict[str, float]) -> list[tuple[str, float]]:
    """Feature contributions summed by readable label, largest first."""
    out: dict[str, float] = {}
    for f, v in contrib.items():
        out[label(f)] = out.get(label(f), 0.0) + v
    return sorted(out.items(), key=lambda kv: -abs(kv[1]))


def point_now(kickoff: datetime, now: datetime) -> str:
    if now >= prekick_time(kickoff):
        return PREKICK
    if now >= final_report_time(kickoff):
        return FINAL_REPORT
    return EARLY


@dataclass
class LivePick:
    game_id: int
    kickoff: datetime
    home: str
    away: str
    point: str
    margin: float
    total: float
    win_prob: float
    market_spread: float | None
    market_total: float | None
    blend_margin: float | None
    blend_total: float | None
    margin_factors: list[tuple[str, float]] = field(default_factory=list)
    total_factors: list[tuple[str, float]] = field(default_factory=list)
    missing: dict[str, list[str]] = field(default_factory=dict)
    stored: bool = False
    ml_home: int | None = None  # market moneyline seen with the prediction (M13)
    ml_away: int | None = None


def version_of(design: Design, fits: dict[str, Fit], model: str = "m12") -> str:
    coef = {p: [list(f.margin.coef), list(f.total.coef)] for p, f in fits.items()}
    blob = json.dumps({"design": design.__dict__, "coef": coef}, default=float, sort_keys=True)
    return f"{model}:{hashlib.sha1(blob.encode()).hexdigest()[:10]}"


def blend(model: float, market: float | None, w: float) -> float | None:
    return None if market is None else w * model + (1 - w) * market


def missing_players(a: AsOf, g, team: int, point: str) -> list[str]:
    """Starters (role >= 0.6) less likely than not to play, by name."""
    from betdata.nfl.features.availability import STARTER, candidates, play_prob

    out = []
    for k, grp, role, bucket in candidates(a, g, team, point):
        p = play_prob(a.store.params, bucket)
        if role >= STARTER and p < 0.5:
            out.append(f"{grp} {a.store.names.get(k, k)} ({100 * p:.0f}%)")
    return sorted(out)


def predict_games(store: Store, design: Design, fits: dict[str, Fit], groups, game_ids,
                  now: datetime, weights: dict[str, float]) -> list[LivePick]:  # fmt: skip
    a = AsOf(store, now)
    picks = []
    for gid in game_ids:
        g = store.games.loc[gid]
        point = point_now(g.kickoff, now)
        fit = fits[PREKICK if point == PREKICK else FINAL_REPORT]
        x: dict[str, float] = {}
        for grp in groups:
            x.update(grp.compute(a, g, FINAL_REPORT if point == EARLY else point))
        margin, total = fit.predict(x)
        m = market_at(a, gid)
        spread = m["spreads"]
        mkt_margin = None if spread is None else -spread
        picks.append(LivePick(
            game_id=int(gid), kickoff=g.kickoff,
            home=store.names.get(f"team:{g.home}", str(g.home)),
            away=store.names.get(f"team:{g.away}", str(g.away)), point=point,
            margin=round(margin, 2), total=round(total, 2),
            win_prob=round(win_prob(margin, fit.sigma), 4),
            market_spread=spread, market_total=m["totals"],
            blend_margin=_r(blend(margin, mkt_margin, weights["margin"])),
            blend_total=_r(blend(total, m["totals"], weights["total"])),
            margin_factors=group_factors(fit.margin.contributions(x)),
            total_factors=group_factors(fit.total.contributions(x)),
            missing={side: missing_players(a, g, team, FINAL_REPORT if point == EARLY else point)
                     for side, team in (("home", g.home), ("away", g.away))},
        ))  # fmt: skip
        picks[-1].features = {k: round(float(v), 4) for k, v in x.items() if v}
    return picks


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 2)


def store_picks(conn: Connection, picks: list[LivePick], version: str, now: datetime,
                model: str = "m12") -> None:  # fmt: skip
    """A new row per game and point whenever the prediction, blend or market (spread, total or
    moneyline) moved."""
    mp = model_predictions
    for p in picks:
        last = conn.execute(
            select(mp.c.pred_home_margin, mp.c.pred_total, mp.c.market_spread_home,
                   mp.c.market_total, mp.c.market_ml_home, mp.c.market_ml_away)
            .where(mp.c.model_version == version, mp.c.game_id == p.game_id,
                   mp.c.prediction_point == p.point)
            .order_by(desc(mp.c.predicted_at)).limit(1)
        ).first()  # fmt: skip
        now_vals = (p.margin, p.total, p.market_spread, p.market_total, p.ml_home, p.ml_away)
        if last is not None and tuple(last) == now_vals:
            continue
        conn.execute(insert(mp).values(
            model=model, model_version=version, game_id=p.game_id, predicted_at=now,
            pred_home_margin=p.margin, pred_total=p.total, pred_home_win_prob=p.win_prob,
            market_spread_home=p.market_spread, market_total=p.market_total,
            market_source="poll" if p.market_spread is not None else None,
            prediction_point=p.point, blend_home_margin=p.blend_margin,
            blend_total=p.blend_total, features=getattr(p, "features", {}),
            factors={"margin": p.margin_factors[:8], "total": p.total_factors[:8],
                     "missing": p.missing},
            params={"design": "config/nfl_model.json" if model == "m12"
                    else "config/nfl_model_wx.json"},
            market_ml_home=p.ml_home, market_ml_away=p.ml_away,
        ))  # fmt: skip
        p.stored = True


def _line(p: LivePick, home_margin: float) -> str:
    """'KC -3.5': the favourite and by how much (a home margin of +3.5 = home -3.5)."""
    if abs(home_margin) < 0.05:
        return "pick'em"
    return f"{p.home if home_margin > 0 else p.away} -{abs(home_margin):.1f}"


def fmt_pick(p: LivePick, top: int = 4) -> list[str]:
    line = _line(p, p.margin)
    mkt = "-" if p.market_spread is None else _line(p, -p.market_spread)
    blend_txt = "" if p.blend_margin is None else f" (blend {_line(p, p.blend_margin)})"
    total_mkt = "-" if p.market_total is None else f"{p.market_total:.1f}"
    blend_t = "" if p.blend_total is None else f" (blend {p.blend_total:.1f})"
    out = [
        f"{p.kickoff:%a %m-%d %H:%M}Z {p.away:>4} @ {p.home:<4} [{p.point}]  line: model {line}"
        f"{blend_txt}, market {mkt}  |  total: model {p.total:.1f}{blend_t}, market {total_mkt}"
        f"  |  {p.home} win {100 * p.win_prob:.0f}%" + ("" if p.stored else "  (unchanged)"),
        "    line factors (points for " + p.home + "): " + ", ".join(
            f"{lab} {v:+.1f}" for lab, v in p.margin_factors[:top]),
        "    total factors: " + ", ".join(f"{lab} {v:+.1f}" for lab, v in p.total_factors[:top]),
    ]  # fmt: skip
    for side, team in (("away", p.away), ("home", p.home)):
        if p.missing.get(side):
            out.append(f"    {team} likely out: " + ", ".join(p.missing[side]))
    return out


def market_weights(design: Design) -> dict[str, float]:
    """Weight of the model in the model+market blend (chosen on validation seasons)."""
    return {"margin": design.blend_margin, "total": design.blend_total}
