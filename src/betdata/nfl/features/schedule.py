"""Group 3: schedule and travel. All of it is fixed by the schedule, known before the season.

Per team: rest days (difference, short week <= 5 days, post-bye >= 13), miles from its home
stadium to the venue, time zones crossed, body clock (kickoff before 11:00 on the team's home
clock, e.g. a West Coast team at 1 pm Eastern), consecutive road games, a sea-level team at
altitude. Per game: international venue. A team's home stadium is the venue of its latest
non-neutral home game.
"""

from __future__ import annotations

import math
from zoneinfo import ZoneInfo

import pandas as pd

from betdata.nfl.store import AsOf

EARTH_MILES = 3958.8
ALTITUDE_M = 1000.0


def miles(lat1, lon1, lat2, lon2) -> float:
    if any(pd.isna(x) for x in (lat1, lon1, lat2, lon2)):
        return 0.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_MILES * math.asin(math.sqrt(h))


def _offset_hours(tz: str | None, when: pd.Timestamp) -> float | None:
    if not isinstance(tz, str) or not tz:
        return None
    off = when.tz_convert(ZoneInfo(tz)).utcoffset()
    return off.total_seconds() / 3600 if off is not None else None


def _schedule_index(games: pd.DataFrame) -> dict:
    """Per game id and team: home venue facts and road streak before that game."""
    g = games.reset_index(drop=True).sort_values(["kickoff", "game_id"])
    home_venue: dict[int, tuple] = {}
    streak: dict[tuple[int, int], int] = {}
    away_run: dict[int, tuple[int, int]] = {}  # team -> (season, consecutive away games)
    venue_of: dict[tuple[int, int], tuple] = {}
    for r in g.itertuples(index=False):
        if not r.neutral and not pd.isna(r.lat):
            home_venue[r.home] = (r.lat, r.lon, r.tz, r.elevation_m)
        for team in (r.home, r.away):
            season, run = away_run.get(team, (r.season, 0))
            if season != r.season:
                run = 0
            is_away = team == r.away and not r.neutral
            run = run + 1 if is_away else 0
            away_run[team] = (r.season, run)
            streak[(r.game_id, team)] = run
            venue_of[(r.game_id, team)] = home_venue.get(team)
    return {"streak": streak, "home_venue": venue_of}


class Schedule:
    name = "sched"
    version = 1

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        idx = a.store.cache.get("sched_index")
        if idx is None:  # schedule facts only: the same for every instant
            idx = a.store.cache["sched_index"] = _schedule_index(a.games)
        vals = {}
        for side, team, rest in (("h", g.home, g.home_rest), ("w", g.away, g.away_rest)):
            hv = idx["home_venue"].get((g.game_id, team))
            travel = miles(hv[0], hv[1], g.lat, g.lon) / 1000 if hv else 0.0
            home_off = _offset_hours(hv[2], g.kickoff) if hv else None
            venue_off = _offset_hours(g.tz, g.kickoff)
            both = home_off is not None and venue_off is not None
            tz = abs(venue_off - home_off) if both else 0.0
            local_home = g.kickoff + pd.Timedelta(hours=home_off) if home_off is not None else None
            body = float(local_home is not None and local_home.tz_localize(None).hour < 11)
            high = not pd.isna(g.elevation_m) and g.elevation_m >= ALTITUDE_M
            alt = float(high and hv is not None and (pd.isna(hv[3]) or hv[3] < ALTITUDE_M))
            vals[side] = {
                "rest": min(rest, 14.0),
                "short": float(rest <= 5),
                "bye": float(rest >= 13),
                "travel": travel,
                "tz": tz,
                "body": body,
                "road": float(max(0, idx["streak"].get((g.game_id, team), 0) - 1)),
                "alt": alt,
            }
        out = {}
        for k in vals["h"]:
            out[f"m_sched_{k}"] = vals["h"][k] - vals["w"][k]
            out[f"t_sched_{k}"] = vals["h"][k] + vals["w"][k]
        intl = float(isinstance(g.country, str) and g.country not in ("US", ""))
        out["m_sched_intl"] = intl
        out["t_sched_intl"] = intl
        return out
