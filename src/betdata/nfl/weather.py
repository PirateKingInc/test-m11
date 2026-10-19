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
import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import Connection, insert, select

from betdata.db.schema import game_source_ids, games, nfl_weather_asof, raw_responses
from betdata.nfl.points import POINTS, point_time

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
