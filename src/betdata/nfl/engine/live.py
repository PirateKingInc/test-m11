"""M17 live: the engine's hourly predictions, `nfle17` (sides) and `nflpe17` (props), beside
the current NFL versions (MODEL.md "M17").

  config/nfl_engine.json         the design: rate and simulation parameters, the frozen fit
                                 file, the validated thresholds (none = the engine flags nothing)
  config/fits/nfle17_*.json      everything learned on completed seasons: the drive model with
                                 its calibration, the role priors, the position means, the
                                 league fallback rates (`betdata nfl engine freeze`)

Each hour, for every scheduled game in the next 8 days at its current point: the as-of rates
from the cached engine tables (`derived/m17/`, the last three seasons), M12's play
probabilities from the live M12 store, the depth chart known now, then 10,000 simulations with
a seed fixed by game and point (same inputs, same numbers, so storage is change-only), the
chain-step decomposition, and P(over) at every stored prop line. No line is an input: the
current line is stored beside the prediction for the page and grading only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import Connection, Engine, desc, insert, select

from betdata.config import PROJECT_ROOT
from betdata.nfl.engine import drives as D
from betdata.nfl.engine import inputs as I
from betdata.nfl.engine import sim as SM
from betdata.nfl.engine.rates import RateParams, RateStore

log = logging.getLogger(__name__)
DESIGN_PATH = PROJECT_ROOT / "config" / "nfl_engine.json"
FIT_DIR = PROJECT_ROOT / "config" / "fits"
MODEL, PROP_MODEL = "nfle17", "nflpe17"
DAYS = 8
LIVE_SEASONS = 2  # earlier seasons read besides the current one


@dataclass
class EngineDesign:
    fit: str  # relative to the project
    rates: dict = field(default_factory=dict)
    sims: int = 10_000
    decomposition_sims: int = 4_000
    spread_threshold: float | None = None  # validated; None = no side flags
    prop_thresholds: dict = field(default_factory=dict)  # market -> edge (probability)
    prop_info_only: list = field(default_factory=list)
    page: bool = False  # the M15 page rule moved the page to the engine
    # side flags need a positive test ROI (the flag rule); picks at the threshold are graded
    # and counted in the season record either way
    flag_sides: bool = False
    side_test_roi: float | None = None

    @classmethod
    def load(cls, path: Path = DESIGN_PATH) -> EngineDesign:
        return cls(**json.loads(path.read_text()))

    def save(self, path: Path = DESIGN_PATH) -> None:
        path.write_text(json.dumps(self.__dict__, indent=2) + "\n")


def version(design: EngineDesign) -> str:
    fit = (PROJECT_ROOT / design.fit).read_bytes()
    h = hashlib.sha1(fit + json.dumps(design.rates, sort_keys=True).encode()).hexdigest()[:10]
    return f"{MODEL}:{h}"


def freeze(seasons, calib_seasons, rates: dict, out: Path | None = None) -> Path:
    """Fit the drive model on `seasons`, calibrate it on `calib_seasons` (the last two), and
    write the frozen fit with the role priors, position means and league fallback rates."""
    from betdata.nfl.engine import backtest as B
    from betdata.nfl.engine.rates import export_priors

    cfg = B.EngineConfig(rates=RateParams(**rates), fit_seasons=tuple(seasons))
    eng = B.Engine(cfg, seasons=range(min(seasons), max(seasons) + 1))
    cal = B.calibrate(eng, seasons=tuple(calib_seasons), rounds=4)
    fit = {"seasons": list(seasons), "calibration_seasons": list(calib_seasons),
           "calibration": cal, "drive_model": eng.model.to_json(), "role_coefs": eng.coefs,
           "priors": export_priors(eng.store)}  # fmt: skip
    out = out or FIT_DIR / f"nfle17_{min(seasons)}_{max(seasons)}.json"
    out.write_text(json.dumps(fit, default=float))
    return out


def load_fit(design: EngineDesign) -> dict:
    return json.loads((PROJECT_ROOT / design.fit).read_text())


def seed_of(game_id: str, point: str) -> int:
    return zlib.crc32(f"{game_id}|{point}".encode())


@dataclass
class EngineRun:
    games: list = field(default_factory=list)  # dicts per game
    props: list = field(default_factory=list)
    seconds: float = 0.0


def live_avail(store, gid_map: dict, now: datetime) -> pd.DataFrame:
    """M12's candidates and play probabilities at each game's current point, from the live
    M12 store (the same rules M12 and nflp15 use)."""
    from betdata.nfl.features.availability import candidates, play_prob
    from betdata.nfl.points import FINAL_REPORT
    from betdata.nfl.predict import EARLY, point_now
    from betdata.nfl.props.panel import healthy_play
    from betdata.nfl.store import AsOf

    healthy = healthy_play(store.params)
    a = AsOf(store, now)
    rows = []
    for gid, nv in gid_map.items():
        g = store.games.loc[gid]
        point = point_now(g.kickoff, now)
        point = FINAL_REPORT if point == EARLY else point
        for team in (g.home, g.away):
            for k, grp, role, bucket in candidates(a, g, team, point):
                p = play_prob(store.params, bucket)
                rows.append((gid, point, team, k, grp, role, bucket, p,
                             max(0.0, min(1.0, 1 - p / healthy[point])), nv))
    return pd.DataFrame(rows, columns=["game_id", "point", "team_id", "player_id", "group",
                                       "role", "bucket", "p_play", "q", "nflverse_game_id"])


def predict(conn: Connection, m12_store, now: datetime,
            design: EngineDesign | None = None) -> EngineRun:  # fmt: skip
    from betdata.core.matching import season_for
    from betdata.nfl.engine import plays as P
    from betdata.nfl.workflow import data_dir

    t0 = time.perf_counter()
    design = design or EngineDesign.load()
    fit = load_fit(design)
    season = season_for("nfl", now)
    tables = P.load(range(season - LIVE_SEASONS, season + 1), data_dir())
    params = RateParams(**design.rates)
    rs = RateStore(tables, params, priors=fit["priors"])
    model = D.DriveModel.from_json(fit["drive_model"])
    ids = I.team_ids(conn.engine)
    g = m12_store.games
    up = [gid for gid in g.index if g.at[gid, "status"] == "scheduled"
          and now < g.at[gid, "kickoff"] <= now + timedelta(days=DAYS)]  # fmt: skip
    nv = _nflverse_ids(conn, up)
    gid_map = {gid: nv[gid] for gid in up if gid in nv and nv[gid] in ids.index}
    if not gid_map:
        return EngineRun(seconds=time.perf_counter() - t0)
    av = live_avail(m12_store, gid_map, now)
    depth = I.Depth.from_frame(_live_depth(conn, season))
    t = pd.Timestamp(now)
    slots = []
    for gid, nvid in gid_map.items():
        h = ids.loc[nvid]
        pt = av[av.game_id == gid].point.iloc[0] if (av.game_id == gid).any() else "final_report"
        slots.append(I.Slot(nvid, pt, t, season, h.home, h.away))
    tr = I.team_rows(rs, slots)
    pr = I.player_rows(rs, av, slots, ids, fit["role_coefs"], depth)
    keys = zip(pr.nflverse_game_id, pr.player_id, strict=True)
    team_of = dict(zip(keys, pr.team_id, strict=True))
    kick, pat = D.kickoff_starts(rs, t), D.pat_rates(rs, t)
    run = EngineRun()
    simp = SM.SimParams(n=design.sims)
    decp = SM.SimParams(n=design.decomposition_sims)
    try:
        lines = _prop_lines(conn, list(gid_map), now, season)
    except Exception as e:  # noqa: BLE001 - props never stop the side predictions
        log.warning("nfle17 prop lines unavailable: %s", e)
        lines = pd.DataFrame()
    inv = {v: k for k, v in gid_map.items()}
    for s in slots:
        seed = seed_of(s.game_id, s.point)
        home, away = I.sides(tr, pr, s, params.qb_weight)
        gs = SM.simulate(model, home, away, kick, pat, simp, seed)
        steps = decompose(model, tr, pr, s, params.qb_weight, kick, pat, decp, seed)
        run.games.append(_game_row(inv[s.game_id], s, gs, steps,
                                   SM.consistency(gs, home, away)))  # fmt: skip
        for p in _props(inv[s.game_id], s, gs, home, away, lines):
            p["team_id"] = team_of.get((s.game_id, p["gsis"]))
            run.props.append(p)
    run.seconds = time.perf_counter() - t0
    return run


def decompose(model, tr, pr, s: I.Slot, qb_weight: float, kick, pat, p: SM.SimParams,
              seed: int) -> dict:  # fmt: skip
    """Each chain step's contribution to the home margin and the total: the full prediction
    minus a re-run with that step at the league average, same random numbers."""
    home, away = I.sides(tr, pr, s, qb_weight)
    base = SM.simulate(model, home, away, kick, pat, p, seed)
    bm, bt = base.margin.mean(), (base.home_pts + base.away_pts).mean()
    out = {}
    for step in I.STEPS:
        if step == "home":
            g = SM.simulate(model, home, away, kick, pat, p, seed, home_field=False)
        else:
            h, a = I.sides(tr, pr, s, qb_weight, frozenset({step}))
            g = SM.simulate(model, h, a, kick, pat, p, seed)
        out[step] = {"margin": round(float(bm - g.margin.mean()), 2),
                     "total": round(float(bt - (g.home_pts + g.away_pts).mean()), 2)}
    return out


def _game_row(gid: int, s: I.Slot, gs: SM.GameSim, steps: dict, checks: dict) -> dict:
    m, tot = gs.margin, gs.home_pts + gs.away_pts
    hist = np.bincount(np.clip(m.astype(int) + 60, 0, 120), minlength=121)
    return {"game_id": int(gid), "nflverse_game_id": s.game_id, "point": s.point,
            "margin": round(float(m.mean()), 2), "total": round(float(tot.mean()), 2),
            "win_prob": round(float((m > 0).mean() + 0.5 * (m == 0).mean()), 4),
            "params": {"margin_median": float(np.median(m)),
                       "margin_p10": float(np.percentile(m, 10)),
                       "margin_p90": float(np.percentile(m, 90)),
                       "total_median": float(np.median(tot)),
                       "total_p10": float(np.percentile(tot, 10)),
                       "total_p90": float(np.percentile(tot, 90)),
                       "margin_hist": hist.tolist(), "sims": int(len(m)),
                       "consistency_ok": all(v <= 1e-6 for v in checks.values())},
            "steps": steps}  # fmt: skip


MARKET_STAT = {"pass_yds": "pass_yards", "rush_yds": "rush_yards", "rec_yds": "rec_yards",
               "receptions": "receptions", "anytime_td": "anytime_td"}


def _props(gid: int, s: I.Slot, gs: SM.GameSim, home, away, lines: pd.DataFrame) -> list:
    out = []
    if lines.empty:
        return out
    ln = lines[lines.game_id == gid]
    for r in ln.itertuples(index=False):
        stat = MARKET_STAT.get(r.market)
        if stat is None or not isinstance(r.gsis, str):
            continue
        if stat == "anytime_td":
            rt = gs.players.get((r.gsis, "rec_td"))
            ru = gs.players.get((r.gsis, "rush_td"))
            if rt is None and ru is None:
                continue
            v = (np.asarray(rt if rt is not None else 0, float)
                 + np.asarray(ru if ru is not None else 0, float))  # fmt: skip
            line = 0.5 if r.line != r.line or r.line is None else float(r.line)
        else:
            v = gs.players.get((r.gsis, stat))
            if v is None or np.ndim(v) == 0 or r.line is None or r.line != r.line:
                continue
            v = np.asarray(v, float)
            line = float(r.line)
        over, under = (v > line).sum(), (v < line).sum()
        p = float(over / (over + under)) if over + under else 0.5
        out.append({"game_id": int(gid), "point": s.point, "gsis": r.gsis, "row": r,
                    "median": float(np.median(v)), "mean": float(v.mean()), "p_over": p,
                    "p10": float(np.percentile(v, 10)), "p90": float(np.percentile(v, 90))})
    return out


def _nflverse_ids(conn: Connection, game_ids) -> dict:
    from betdata.db.schema import game_source_ids

    rows = conn.execute(select(game_source_ids.c.game_id, game_source_ids.c.source_game_id)
                        .where(game_source_ids.c.source == "nflverse",
                               game_source_ids.c.game_id.in_([int(g) for g in game_ids])))
    return {g: s for g, s in rows}


def _live_depth(conn: Connection, season: int) -> pd.DataFrame:
    """Depth charts known now: the cached m15 file of this season (daily ESPN-based charts),
    else the raw nflverse file."""
    from betdata.nfl.m15cache import cached_depth
    from betdata.nfl.workflow import data_dir

    try:
        return cached_depth(data_dir(), season, None)
    except Exception as e:  # noqa: BLE001 - no chart: M12 candidates are used as they are
        log.warning("nfle17 depth charts unavailable: %s", e)
        return pd.DataFrame()


def _prop_lines(conn: Connection, game_ids, now: datetime, season: int) -> pd.DataFrame:
    from betdata.nfl.props.ids import espn_to_gsis
    from betdata.nfl.props.live import latest_lines
    from betdata.nfl.workflow import data_dir

    lines = latest_lines(conn, game_ids, now)
    lines = lines[lines.market.isin(list(MARKET_STAT))].copy()
    if lines.empty:
        return lines
    stats = pd.DataFrame(columns=["player_id", "name"])
    lines["gsis"] = espn_to_gsis(lines, data_dir(), (season - 1, season), stats)
    names = roster_names(conn, season - 1)
    lines["player_name"] = lines.player_name.fillna(lines.gsis.map(names))  # NFL lines: no name
    return lines


def roster_names(conn: Connection, first_season: int) -> dict[str, str]:
    """gsis id -> name from the stored rosters (the latest season's spelling wins)."""
    from betdata.db.schema import roster_weeks as rw

    rows = conn.execute(select(rw.c.gsis_id, rw.c.player_name)
                        .where(rw.c.season >= first_season, rw.c.gsis_id.is_not(None))
                        .order_by(rw.c.season, rw.c.week))  # fmt: skip
    return {g: n for g, n in rows}


def store(conn: Connection, run: EngineRun, now: datetime, design: EngineDesign) -> dict:
    """Change-only, as the other versions: a new row when the prediction or the market moved.
    Props go to prop_predictions (nflpe17) through the shared prop store."""
    from betdata.db.schema import model_predictions as mp
    from betdata.moneyline import no_vig
    from betdata.nba.props.live import store as store_props
    from betdata.quotes import current_quotes

    ver = version(design)
    quotes = current_quotes(conn, [g["game_id"] for g in run.games], now)
    n = 0
    for g in run.games:
        q = quotes.get(g["game_id"], {})
        sp, to = q.get("spread_home"), q.get("total")
        spread = None if sp is None else sp.line
        total = None if to is None else to.line
        last = conn.execute(select(mp.c.pred_home_margin, mp.c.pred_total,
                                   mp.c.market_spread_home, mp.c.market_total)
                            .where(mp.c.model_version == ver, mp.c.game_id == g["game_id"],
                                   mp.c.prediction_point == g["point"])
                            .order_by(desc(mp.c.predicted_at)).limit(1)).first()  # fmt: skip
        vals = (g["margin"], g["total"], spread, total)
        if last is not None and tuple(last) == vals:
            continue
        conn.execute(insert(mp).values(
            model=MODEL, model_version=ver, game_id=g["game_id"], predicted_at=now,
            pred_home_margin=g["margin"], pred_total=g["total"], pred_home_win_prob=g["win_prob"],
            market_spread_home=spread, market_total=total,
            market_source="poll" if spread is not None else None, prediction_point=g["point"],
            params={**g["params"], "design": "config/nfl_engine.json"},
            factors={"steps": g["steps"]}))  # fmt: skip
        n += 1
    picks = []
    pver = ver.replace(MODEL, PROP_MODEL)
    for p in run.props:
        r = p["row"]
        nv = (no_vig(r.over_price, r.under_price) if pd.notna(r.over_price)
              and pd.notna(r.under_price) else None)  # fmt: skip
        picks.append(dict(
            model=PROP_MODEL, model_version=pver, game_id=p["game_id"], player_id=str(r.player_id),
            player_name=r.player_name or str(r.player_id),
            team_id=None if p["team_id"] is None else int(p["team_id"]),
            market=r.market, prediction_point=p["point"], mean=round(p["median"], 2),
            line=None if pd.isna(r.line) else float(r.line), over_price=_int(r.over_price),
            under_price=_int(r.under_price), book_id=int(r.book_id),
            line_fetched_at=r.fetched_at.to_pydatetime(), p_over=round(p["p_over"], 4),
            edge=round(100 * (p["p_over"] - nv[0]), 1) if nv else None,
            dist={"engine": MODEL, "mean": round(p["mean"], 2), "p10": p["p10"],
                  "p90": p["p90"], "gsis_id": p["gsis"]},
            factors=[]))  # fmt: skip
    m = store_props(conn, picks, now) if picks else 0
    return {"games": n, "props": m}


def _int(x):
    return None if x is None or pd.isna(x) else int(x)


def run(engine: Engine, now: datetime, m12_store=None) -> dict:
    """The hourly entry point (NFL plugin). Never raises into the run."""
    if not DESIGN_PATH.exists():
        return {"skipped": "no design"}
    from betdata.nfl.live import loaded_store

    design = EngineDesign.load()
    m12_store = m12_store if m12_store is not None else loaded_store(engine)
    with engine.connect() as conn:
        res = predict(conn, m12_store, now, design)
    with engine.begin() as conn:
        out = store(conn, res, now, design)
    out["seconds"] = round(res.seconds, 1)
    out["predicted"] = len(res.games)
    out["prop_lines"] = len(res.props)
    log.info("nfle17: %s", out)
    return out
