"""M13 weather for the NFL model: game weather as it could be known at each prediction point.

Sources, in order of preference for a game and point (table `nfl_weather_asof`):
  open_meteo_prev  Open-Meteo previous-runs forecast from the run `lead_days` before the game's
                   hours, with N chosen so the run was out before the prediction point
                   (lead_days = ceil((last game hour - point + 6 h model latency) / 24 h)).
                   2022 onward, where Open-Meteo keeps it.
  open_meteo_hf    Open-Meteo historical-forecast archive: built from the most recent run
                   before each hour, so a short-lead forecast that may have been issued after
                   the prediction point. A fallback only, flagged by its source.
  nflverse_obs     nflverse games.csv `temp` (F) and `wind` (mph): the weather observed at the
                   game, 1999-2021, as a stand-in for a forecast where none was archived. Its
                   known_at is the prediction point by convention (MODEL.md, As-of rules).
Live games use the latest Open-Meteo forecast the hourly runner stored (`weather` frame).

Every value covers the kickoff hour plus the next three (UTC): mean temperature, wind and gust,
total precipitation. Domes are skipped (the feature group zeroes indoor games).
"""

from __future__ import annotations

import gzip
import io
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import Connection, insert, select

from betdata.config import PROJECT_ROOT
from betdata.db.schema import game_source_ids, games, nfl_weather_asof, raw_responses
from betdata.nfl.points import POINTS, point_time

WX_DESIGN_PATH = PROJECT_ROOT / "config" / "nfl_model_wx.json"  # the weather model version
GAME_HOURS = 4
MODEL_LATENCY = timedelta(hours=6)  # a run is published a few hours after its start time
MAX_LEAD_DAYS = 3
OBSERVED_LAST_SEASON = 2021
FORECAST_FIRST_SEASON = 2022
HOURLY = ("temperature_2m", "wind_speed_10m", "wind_gusts_10m", "precipitation")
COLUMNS = {"temperature_2m": "temp_c", "wind_speed_10m": "wind_kph",
           "wind_gusts_10m": "gust_kph", "precipitation": "precip_mm"}  # fmt: skip
SOURCE_RANK = {"open_meteo_prev": 0, "open_meteo_hf": 1, "nflverse_obs": 2}


def kickoff_hour(t: datetime) -> datetime:
    return pd.Timestamp(t).tz_convert("UTC").floor("h").to_pydatetime()


def lead_days(kickoff: datetime, point: str) -> int:
    """Smallest whole-day lead whose run was published before the prediction point, for every
    game hour."""
    last_hour = kickoff_hour(kickoff) + timedelta(hours=GAME_HOURS - 1)
    gap = last_hour - point_time(point, kickoff) + MODEL_LATENCY
    return max(1, math.ceil(gap / timedelta(days=1)))


def summarise(hours: list[dict]) -> dict | None:
    """Mean temp/wind/gust and total precipitation over the game's hours (None if empty)."""
    out = {}
    for col in COLUMNS.values():
        vals = [h[col] for h in hours if h.get(col) is not None]
        if not vals:
            out[col] = None
        else:
            out[col] = float(np.sum(vals)) if col == "precip_mm" else float(np.mean(vals))
    return None if all(v is None for v in out.values()) else out


def from_response(resp: dict, kickoff: datetime, suffix: str = "") -> dict | None:
    """The game's hours from one Open-Meteo location response (GMT), for variables with an
    optional suffix such as `_previous_day2`."""
    if resp.get("utc_offset_seconds", 0) != 0:
        raise ValueError(f"expected GMT times, got offset {resp['utc_offset_seconds']}")
    hourly = resp["hourly"]
    index = {t: i for i, t in enumerate(hourly["time"])}
    start = kickoff_hour(kickoff)
    hours = []
    for h in range(GAME_HOURS):
        i = index.get((start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M"))
        if i is None:
            continue
        hours.append({col: (hourly.get(var + suffix) or [None] * (i + 1))[i]
                      for var, col in COLUMNS.items()})  # fmt: skip
    return summarise(hours)


def forecast_rows(game_id: int, kickoff: datetime, hf: dict | None, prev: dict | None,
                  fetched_at: datetime, raw_id: int | None) -> list[dict]:  # fmt: skip
    """Rows for both points from one historical-forecast and one previous-runs response."""
    out = []
    for point in POINTS:
        base = dict(game_id=game_id, point=point, known_at=point_time(point, kickoff),
                    fetched_at=fetched_at, raw_id=raw_id)  # fmt: skip
        n = lead_days(kickoff, point)
        if prev is not None and n <= MAX_LEAD_DAYS:
            vals = from_response(prev, kickoff, f"_previous_day{n}")
            if vals is not None:
                out.append({**base, **vals, "source": "open_meteo_prev", "lead_days": n})
        if hf is not None:
            vals = from_response(hf, kickoff)
            if vals is not None:
                out.append({**base, **vals, "source": "open_meteo_hf", "lead_days": None})
    return out


def insert_new(conn: Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    have = set(conn.execute(
        select(nfl_weather_asof.c.game_id, nfl_weather_asof.c.point, nfl_weather_asof.c.source)
        .where(nfl_weather_asof.c.game_id.in_({r["game_id"] for r in rows}))
    ).all())  # fmt: skip
    new = [r for r in rows if (r["game_id"], r["point"], r["source"]) not in have]
    if new:
        conn.execute(insert(nfl_weather_asof), new)
    return len(new)


# --- nflverse observed weather (stand-in, 1999-2021) -----------------------------------------


def load_observed(conn: Connection, last_season: int = OBSERVED_LAST_SEASON) -> int:
    """Game-day temp/wind from the newest archived nflverse games.csv (no network)."""
    raw = conn.execute(
        select(raw_responses.c.raw_id, raw_responses.c.body_gz, raw_responses.c.fetched_at)
        .where(raw_responses.c.source == "nflverse",
               raw_responses.c.endpoint == "/schedules/games.csv")
        .order_by(raw_responses.c.fetched_at.desc()).limit(1)
    ).first()  # fmt: skip
    if raw is None:
        return 0
    df = pd.read_csv(io.BytesIO(gzip.decompress(raw.body_gz)), low_memory=False)
    df = df[(df.season <= last_season) & df.temp.notna() & df.wind.notna()
            & df.roof.isin(["outdoors", "open"])]  # fmt: skip
    ids = dict(conn.execute(
        select(game_source_ids.c.source_game_id, game_source_ids.c.game_id)
        .where(game_source_ids.c.source == "nflverse")).all())  # fmt: skip
    kicks = dict(conn.execute(select(games.c.game_id, games.c.start_time_utc)).all())
    rows = []
    for r in df.itertuples(index=False):
        gid = ids.get(r.game_id)
        if gid is None or gid not in kicks:
            continue
        for point in POINTS:
            rows.append(dict(
                game_id=gid, point=point, source="nflverse_obs", lead_days=None,
                temp_c=round((float(r.temp) - 32) * 5 / 9, 2),
                wind_kph=round(float(r.wind) * 1.609344, 2), gust_kph=None, precip_mm=None,
                known_at=point_time(point, kicks[gid]), fetched_at=raw.fetched_at,
                raw_id=raw.raw_id))  # fmt: skip
    return insert_new(conn, rows)


# --- evaluation (validation 2020-2022) and the single final test ------------------------------


def wx_rows(store, design, seasons, refresh: bool = True):
    """The design's cached rows plus the weather group (rebuilt: its data grows with backfills)."""
    from betdata.nfl.groups import GROUPS
    from betdata.nfl.workflow import design_rows, feature_rows, model_games

    rows = design_rows(store, design, seasons)
    wx = feature_rows(store, GROUPS["wx"], model_games(store, seasons), refresh=refresh)
    return rows.merge(wx.drop(columns=["season", "week"]), on=["game_id", "point"])


def coverage(store) -> dict:
    """Weather rows per season and source (both points)."""
    df = store.frame("weather_asof").df
    if df.empty:
        return {}
    df = df.merge(store.games[["season"]], left_on="game_id", right_index=True)
    return {f"{s}:{src}": int(n) for (s, src), n in df.groupby(["season", "source"]).size().items()}


def evaluate_weather(rows, out, design) -> tuple[dict, object]:
    """The M12 design with and without the weather group, M12's penalties kept (nothing is
    re-tuned). The group joins the margin and/or total model only where it lowers the
    validation error, as every M12 group did."""
    from betdata.nfl.groups import columns
    from betdata.nfl.pipeline import evaluate
    from betdata.nfl.selection import Design

    m, t = design.columns(rows.columns)
    wm, wt = columns(rows.columns, "wx")
    lam = dict(lambdas=(design.lam_margin,), lambdas_t=(design.lam_total,))
    base = evaluate(rows, out, m, t, **lam)
    with_m = evaluate(rows, out, m + wm, t, **lam)
    with_t = evaluate(rows, out, m, t + wt, **lam)
    keep_m, keep_t = with_m.margin < base.margin, with_t.total < base.total
    new = Design(design.margin_groups + (["wx"] if keep_m else []),
                 design.total_groups + (["wx"] if keep_t else []), design.lam_margin,
                 design.lam_total, design.model, design.blend_margin,
                 design.blend_total)  # fmt: skip
    report = {
        "m12": {"margin": base.margin, "total": base.total, "valid": base.valid},
        "with_wx_margin": {"margin": with_m.margin, "valid": with_m.valid},
        "with_wx_total": {"total": with_t.total, "valid": with_t.valid},
        "keep_margin": bool(keep_m), "keep_total": bool(keep_t),
        "design": new.__dict__,
    }  # fmt: skip
    return report, new


def final_test_weather(rows, out, design_m12, design_wx, games_by_id: dict,
                       report_path: Path, force: bool = False) -> dict:  # fmt: skip
    """Both designs refitted on 2013-2022 and scored once on 2023-2025: margin and total error,
    and totals bets against the close at 4 points (all games, and outdoor games with weather)."""
    from betdata.nfl.final import bets_at_close, tally
    from betdata.nfl.pipeline import TEST, TRAIN, VALIDATE
    from betdata.nfl.selection import fit_design, predict_rows

    if report_path.exists() and not force:
        raise RuntimeError(f"test seasons already scored ({report_path}); they are scored once")
    test = rows[rows.season.isin(TEST)]
    wx_cols = [c for c in test.columns if c.startswith("t_wx_")]
    have_wx = set(test.loc[(test[wx_cols] != 0).any(axis=1), "game_id"]) if wx_cols else set()
    report: dict = {"seasons": list(TEST), "designs": {"m12": design_m12.__dict__,
                                                       "wx": design_wx.__dict__}, "points": {}}
    preds = {name: predict_rows(test, fit_design(rows, out, d, (*TRAIN, *VALIDATE)))
             for name, d in (("m12", design_m12), ("wx", design_wx))}  # fmt: skip
    for point in POINTS:
        res = {}
        for name, pred in preds.items():
            p = pred[pred.point == point].merge(
                out.rename(columns={"margin": "am", "total": "at"}), left_on="game_id",
                right_index=True)  # fmt: skip
            sub = p[p.game_id.isin(have_wx)]
            bets_all = [bets_at_close(None, r.total, None, games_by_id[r.game_id].total,
                                      games_by_id[r.game_id].home_score,
                                      games_by_id[r.game_id].away_score)["total"]
                        for r in p.itertuples() if games_by_id[r.game_id].total is not None]
            # fmt: skip
            res[name] = {
                "games": int(len(p)),
                "margin_mae": float((p.margin - p.am).abs().mean()),
                "total_mae": float((p.total - p["at"]).abs().mean()),
                "weather_games": int(len(sub)),
                "total_mae_weather_games": float((sub.total - sub["at"]).abs().mean())
                if len(sub) else None,
                "total_bets": tally(f"{name} total", bets_all).as_dict(),
            }  # fmt: skip
        report["points"][point] = res
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=1, default=float))
    return report
