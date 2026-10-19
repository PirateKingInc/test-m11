"""M13 weather group (`wx`): the game's weather as it could be known at the prediction point.

History: the best `nfl_weather_asof` row for the game and point (Open-Meteo forecast issued
before the point, else the historical-forecast archive, else nflverse's observed game-day
weather as a stand-in for 1999-2021; nfl/weather.py). Live: the latest Open-Meteo forecast the
runner stored by then. Outdoor share as in group 4 (domes indoor; a retractable roof's state
known on game day). Wind above 10 and above 25 km/h, cold below 5 C, heat above 27 C, rain.
No "weather known" flag: coverage is not weather, and a game without data is scored as calm.
"""

from __future__ import annotations

import pandas as pd

from betdata.nfl.features.environment import Environment, latest_forecast
from betdata.nfl.store import AsOf

RANK = {"open_meteo_prev": 0, "open_meteo_hf": 1, "nflverse_obs": 2}


def weather_at(a: AsOf, game_id: int, point: str) -> dict | None:
    if "weather_asof" in a.store.frames:
        w = a.df("weather_asof")
        rows = w[(w.game_id == game_id) & (w.point == point)]
        if len(rows):
            return min((r for _, r in rows.iterrows()),
                       key=lambda r: RANK.get(r.source, 9)).to_dict()  # fmt: skip
    return latest_forecast(a, game_id)


def _num(x) -> float:
    return 0.0 if x is None or pd.isna(x) else float(x)


class Weather:
    name = "wx"
    version = 1

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        outdoor = 1.0 - Environment().compute(a, g, point)["t_env_indoor"]
        w = weather_at(a, g.game_id, point) if outdoor > 0 else None
        if w is None:
            wind = strong = cold = hot = rain = 0.0
        else:
            k = _num(w.get("wind_kph"))
            temp = w.get("temp_c")
            wind = outdoor * max(0.0, k - 10.0)
            strong = outdoor * max(0.0, k - 25.0)
            cold = outdoor * max(0.0, 5.0 - temp) if temp is not None and not pd.isna(temp) \
                else 0.0  # fmt: skip
            hot = outdoor * max(0.0, temp - 27.0) if temp is not None and not pd.isna(temp) \
                else 0.0  # fmt: skip
            rain = outdoor * _num(w.get("precip_mm"))
        return {
            "m_wx_wind": wind, "m_wx_cold": cold,
            "t_wx_wind": wind, "t_wx_strong": strong, "t_wx_cold": cold, "t_wx_hot": hot,
            "t_wx_rain": rain,
        }  # fmt: skip
