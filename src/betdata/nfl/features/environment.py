"""Group 4: environment.

Weather is the latest stored Open-Meteo *forecast* fetched by the prediction point (never the
observed weather), averaged over the game's hours: wind above 10 km/h, cold below 5 C, rain.
Indoor games get no weather. Roof: domes are indoor; a retractable roof's state is known only on
game day (prekick), before that it counts as half indoor. Surface: artificial turf or grass.
"""

from __future__ import annotations

import pandas as pd

from betdata.nfl.store import AsOf

TURF = ("astroturf", "fieldturf", "sportturf", "matrixturf", "a_turf", "astroplay")


def latest_forecast(a: AsOf, game_id: int) -> dict | None:
    w = a.df("weather")
    rows = w[w.game_id == game_id]
    if rows.empty:
        return None
    return rows.iloc[-1].to_dict()  # frame is in known_at order: the newest run


class Environment:
    name = "env"
    version = 1

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        if g.roof_type == "dome":
            indoor = 1.0
        elif g.roof_type == "retractable":
            day = a.df("gameday")
            state = day.loc[day.game_id == g.game_id, "roof_state"]
            known = state.iloc[-1] if len(state) and isinstance(state.iloc[-1], str) else None
            indoor = 0.5 if known is None else float(known == "closed")
        else:
            indoor = 0.0
        turf = float(isinstance(g.surface, str) and g.surface.strip().lower() in TURF)
        f = latest_forecast(a, g.game_id)
        outdoor = 1.0 - indoor
        if f is None or outdoor == 0:
            wind = cold = rain = 0.0
            have = 0.0
        else:
            wind = outdoor * max(0.0, (f.get("wind_kph") or 0.0) - 10.0)
            temp = f.get("temp_c")
            cold = outdoor * max(0.0, 5.0 - temp) if not pd.isna(temp) else 0.0
            rain = outdoor * (f.get("precip_mm") or 0.0)
            have = 1.0
        return {
            "t_env_indoor": indoor,
            "t_env_turf": turf,
            "t_env_wind": wind,
            "t_env_cold": cold,
            "t_env_rain": rain,
            "t_env_forecast": have,
        }
