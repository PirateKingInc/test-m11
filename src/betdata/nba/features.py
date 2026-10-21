"""M14 NBA feature groups (MODEL.md, M14 "Feature groups"). Each group's `compute(a, g, point)`
returns margin features `m_<group>_*` and total features `t_<group>_*`, read through `a` (the
store as of the prediction point) or from the schedule (known in advance).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from betdata.nba.state import League, elo_margin, made_by_game, win_pct
from betdata.nfl.features.state import advanced
from betdata.nfl.store import AsOf, Store

# Arena (lat, lon, IANA zone, altitude m) by team abbreviation.
GEO = {
    "ATL": (33.757, -84.396, "America/New_York", 320),
    "BOS": (42.366, -71.062, "America/New_York", 5),
    "BKN": (40.683, -73.976, "America/New_York", 10),
    "CHA": (35.225, -80.839, "America/New_York", 230),
    "CHI": (41.881, -87.674, "America/Chicago", 180),
    "CLE": (41.497, -81.688, "America/New_York", 200),
    "DAL": (32.790, -96.810, "America/Chicago", 130),
    "DEN": (39.749, -105.008, "America/Denver", 1609),
    "DET": (42.341, -83.055, "America/New_York", 190),
    "GSW": (37.768, -122.388, "America/Los_Angeles", 5),
    "HOU": (29.751, -95.362, "America/Chicago", 15),
    "IND": (39.764, -86.156, "America/New_York", 220),
    "LAC": (34.043, -118.267, "America/Los_Angeles", 90),
    "LAL": (34.043, -118.267, "America/Los_Angeles", 90),
    "MEM": (35.138, -90.051, "America/Chicago", 80),
    "MIA": (25.781, -80.188, "America/New_York", 2),
    "MIL": (43.045, -87.917, "America/Chicago", 190),
    "MIN": (44.980, -93.276, "America/Chicago", 260),
    "NOP": (29.949, -90.082, "America/Chicago", 1),
    "NYK": (40.751, -73.993, "America/New_York", 10),
    "OKC": (35.463, -97.515, "America/Chicago", 370),
    "ORL": (28.539, -81.384, "America/New_York", 30),
    "PHI": (39.901, -75.172, "America/New_York", 5),
    "PHX": (33.446, -112.071, "America/Phoenix", 331),
    "POR": (45.532, -122.667, "America/Los_Angeles", 15),
    "SAC": (38.580, -121.500, "America/Los_Angeles", 9),
    "SAS": (29.427, -98.438, "America/Chicago", 200),
    "TOR": (43.643, -79.379, "America/Toronto", 80),
    "UTA": (40.768, -111.901, "America/Denver", 1288),
    "WAS": (38.898, -77.021, "America/New_York", 10),
}
ORLANDO = (28.37, -81.56, "America/New_York", 30)  # the 2020 bubble
LAS_VEGAS = (36.10, -115.18, "America/Los_Angeles", 610)  # In-Season Tournament knockouts
BUBBLE = (date(2020, 7, 1), date(2020, 10, 15))
HIGH = 1000  # metres: DEN and UTA


def _km(p, q) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (p[0], p[1], q[0], q[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(
        (lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _offset(zone: str, t: pd.Timestamp) -> float:
    return t.tz_convert(zone).utcoffset().total_seconds() / 3600


def site(g) -> tuple:
    day = g.tip.tz_convert("America/New_York").date()
    if g.neutral and BUBBLE[0] <= day <= BUBBLE[1]:
        return ORLANDO
    if g.neutral and day.month == 12 and g.season >= 2023:
        return LAS_VEGAS
    return GEO.get(g.home_abbr, ORLANDO)


def schedule_table(store: Store) -> pd.DataFrame:
    """Per (game, team): rest, back-to-backs, density, travel, time zones, altitude, road
    trips. Schedule facts only (tip times and sites are published before the season)."""
    if "schedule" in store.cache:
        return store.cache["schedule"]
    g = store.games
    rows = []
    for side in ("home", "away"):
        part = g[["game_id", "season", "tip", side, "neutral", "home_abbr"]].rename(
            columns={side: "team"})
        part["is_home"] = side == "home" and True
        part["site"] = [site(r) for r in g.itertuples()]
        part["abbr"] = g[f"{side}_abbr"].values
        rows.append(part)
    long = pd.concat(rows).sort_values(["team", "tip"]).reset_index(drop=True)
    out = {}
    for team, grp in long.groupby("team"):
        tips = list(grp.tip)
        sites = list(grp.site)
        homes = [bool(h) and not n for h, n in zip(grp.is_home, grp.neutral, strict=True)]
        seasons = list(grp.season)
        trip = 0
        for i, gid in enumerate(grp.game_id):
            same = i > 0 and seasons[i - 1] == seasons[i]
            rest = (tips[i] - tips[i - 1]).total_seconds() / 86400 if same else 7.0
            nxt = (tips[i + 1] - tips[i]).total_seconds() / 86400 if i + 1 < len(tips) and \
                seasons[i + 1] == seasons[i] else 7.0
            last4 = sum(1 for j in range(max(0, i - 3), i)
                        if seasons[j] == seasons[i] and (tips[i] - tips[j]).days < 4)  # fmt: skip
            last6 = sum(1 for j in range(max(0, i - 5), i)
                        if seasons[j] == seasons[i] and (tips[i] - tips[j]).days < 6)  # fmt: skip
            prev_site = sites[i - 1] if same else sites[i]
            km = _km(prev_site, sites[i]) if same else 0.0
            tz = abs(_offset(sites[i][2], tips[i]) - _offset(prev_site[2], tips[i])) if same \
                else 0.0
            home_geo = GEO.get(grp.abbr.iloc[i], ORLANDO)
            alt = 1.0 if sites[i][3] >= HIGH and home_geo[3] < HIGH else 0.0
            first_home = 1.0 if homes[i] and trip >= 3 else 0.0
            trip = 0 if homes[i] else (trip + 1 if same else 1)
            out[(gid, team)] = dict(rest=min(rest, 5.0), b2b=float(rest < 1.5),
                                    b2b_first=float(nxt < 1.5), in4=float(last4 >= 2),
                                    in6=float(last6 >= 3), km=km / 1000, tz=tz, alt=alt,
                                    trip=float(trip), first_home=first_home)  # fmt: skip
    store.cache["schedule"] = out
    return out


def _state(a: AsOf) -> League:
    return advanced(a, "league", League)


# --- availability (group 1) ---------------------------------------------------------------

STATUSES = ("Out", "Doubtful", "Questionable", "Probable", "Available", "Removed")
DEFAULT_PLAY = {"Out": 0.0, "Doubtful": 0.05, "Questionable": 0.55, "Probable": 0.93,
                "Available": 0.98, "Removed": 0.97}  # fmt: skip


def streak_bucket(n: int) -> str:
    return "0" if n == 0 else "1" if n == 1 else "2-4" if n <= 4 else "5+"


def status_key(status: str | None, streak: int, reported: bool) -> str:
    if status in STATUSES:
        return status
    return f"none|{'rep' if reported else 'norep'}|{streak_bucket(streak)}"


def team_status(a: AsOf, g, team: int) -> tuple[dict[str, str], bool]:
    """Latest known report status per player of `team` for game g, and whether the team was on
    any report for this game yet."""
    rows = advanced(a, "inj", made_by_game("injuries")).rows.get(g.game_id, [])
    out = {}
    reported = False
    for r in rows:
        if r.team_id == team:
            reported = True
            if r.player_id:
                out[r.player_id] = r.status
    return out, reported


def play_prob(a: AsOf, key: str) -> float:
    table = a.store.params.get("play_prob") or {}
    if key in table:
        return table[key]
    if key in DEFAULT_PLAY:
        return DEFAULT_PLAY[key]
    return {"0": 0.97, "1": 0.6, "2-4": 0.35, "5+": 0.12}[key.rsplit("|", 1)[1]]


def streak_for(st: League, g, p) -> int:
    """Games missed in a row, counted within the game's season: before a team's first game of
    a new season, last season's end-of-year absences (rest, tanking, injuries since healed)
    say nothing (REVIEW.md M14-4)."""
    return 0 if st.season is not None and g.season > st.season else p.streak


def availability(a: AsOf, g, team: int) -> dict[str, float]:
    st = _state(a)
    status, reported = team_status(a, g, team)
    rot = st.rotation(team)
    impacts = []
    out = dict(new=0.0, new_pm=0.0, long=0.0, back=0.0, n_out=0.0, star=0.0, q=0.0)
    lineup = []
    for pid, p in rot:
        vg, vpm = p.value()
        share = p.min_play / 48
        streak = streak_for(st, g, p)
        prob = play_prob(a, status_key(status.get(pid), streak, reported))
        lineup.append((prob * p.min_play, vg, vpm))
        impacts.append((share * vg, prob))
        if streak < 3:
            out["new"] += (1 - prob) * share * vg
            out["new_pm"] += (1 - prob) * share * vpm
        else:
            out["long"] += (1 - prob) * share * vg
            out["back"] += prob * share * vg
        if p.min_play >= 20:
            out["n_out"] += 1 - prob
        if status.get(pid) in ("Questionable", "Doubtful"):
            out["q"] += share * vg
    for imp, prob in sorted(impacts, reverse=True)[:2]:
        out["star"] += (1 - prob) * imp
    # The expected lineup: expected minutes scaled to a full game (240), times player value.
    total = sum(m for m, _, _ in lineup) or 1.0
    out["lineup_g"] = sum(m * 240 / total / 48 * vg for m, vg, _ in lineup)
    out["lineup_pm"] = sum(m * 240 / total / 48 * vpm for m, _, vpm in lineup)
    return out


@dataclass(frozen=True)
class Base:
    name: str = "base"

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        st = _state(a)
        home = 0.0 if g.neutral else 1.0
        return {"m_base_home": home,
                "m_base_elo": elo_margin(st, g) - home * 100 / 28,
                "t_base_league": st.league_total}  # fmt: skip


@dataclass(frozen=True)
class Availability:
    name: str = "avail"

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        h, w = availability(a, g, g.home), availability(a, g, g.away)
        out = {f"m_avail_{k}": h[k] - w[k] for k in h if not k.startswith("lineup")}
        out["m_avail_multi"] = h["n_out"] ** 2 - w["n_out"] ** 2
        out["t_avail_lost"] = h["new"] + w["new"]
        return out


@dataclass(frozen=True)
class Strength:
    name: str = "str"

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        st = _state(a)
        oh, dh = st.net(g.home, g.season)
        oa, da = st.net(g.away, g.season)
        foh, fdh = st.net(g.home, g.season, fast=True)
        foa, fda = st.net(g.away, g.season, fast=True)
        pace = st.league_pace / 100
        h, w = availability(a, g, g.home), availability(a, g, g.away)
        return {"m_str_net": ((oh - dh) - (oa - da)) * pace,
                "m_str_fast": ((foh - fdh) - (foa - fda)) * pace,
                "m_str_lineup_g": h["lineup_g"] - w["lineup_g"],
                "m_str_lineup_pm": h["lineup_pm"] - w["lineup_pm"],
                "t_str_off": (oh + da + oa + dh) * pace}  # fmt: skip


@dataclass(frozen=True)
class Rest:
    name: str = "rest"

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        s = schedule_table(a.store)
        h, w = s[(g.game_id, g.home)], s[(g.game_id, g.away)]
        out = {"m_rest_diff": h["rest"] - w["rest"]}
        for k in ("b2b", "b2b_first", "in4", "in6", "km", "tz"):
            out[f"m_rest_{k}"] = h[k] - w[k]
        out["m_rest_alt"] = w["alt"] - h["alt"]
        out["m_rest_trip"] = w["trip"]
        out["m_rest_first_home"] = h["first_home"]
        out["t_rest_b2b"] = h["b2b"] + w["b2b"]
        return out


@dataclass(frozen=True)
class Pace:
    name: str = "pace"

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        st = _state(a)
        return {"t_pace_teams": st.pace.get(g.home, 0.0) + st.pace.get(g.away, 0.0),
                "t_pace_league": st.league_pace, "t_pace_ppp": st.league_ppp}  # fmt: skip


@dataclass(frozen=True)
class HomeCourt:
    name: str = "hca"

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        st = _state(a)
        home = 0.0 if g.neutral else 1.0
        day = g.tip.tz_convert("America/New_York").date()
        return {"m_hca_est": home * st.hca,
                "m_hca_bubble": float(BUBBLE[0] <= day <= BUBBLE[1]),
                "m_hca_crowds": home * float(g.season == 2020)}  # fmt: skip


@dataclass(frozen=True)
class Context:
    name: str = "ctx"

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        st = _state(a)
        gp_h, gp_w = st.gp.get(g.home, 0), st.gp.get(g.away, 0)
        early = math.exp(-min(gp_h, gp_w) / 12)
        prev = st.prev_net.get(g.home, 0.0) - st.prev_net.get(g.away, 0.0)

        def kept(team):
            prev_min = st.prev_roster_min.get(team, {})
            rot = st.rotation(team)
            tot = sum(p.min_play for _, p in rot) or 1.0
            return sum(p.min_play for pid, p in rot if prev_min.get(pid, 0) >= 500) / tot

        reg = g.season_type == "REG"
        late_h = np.clip((gp_h - 65) / 17, 0, 1) if reg else 0.0
        late_w = np.clip((gp_w - 65) / 17, 0, 1) if reg else 0.0
        wh, ww = win_pct(st, g.home), win_pct(st, g.away)
        return {"m_ctx_prior": early * prev * st.league_pace / 100,
                "m_ctx_kept": early * (kept(g.home) - kept(g.away)),
                "m_ctx_tank": late_w * (ww < 0.35) - late_h * (wh < 0.35),
                "m_ctx_rest": late_w * (ww > 0.68) - late_h * (wh > 0.68),
                "m_ctx_post": float(g.season_type == "POST") * (wh - ww)}  # fmt: skip


@dataclass(frozen=True)
class Market:
    name: str = "mkt"

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        rows = advanced(a, "odds", made_by_game("odds")).rows.get(g.game_id, [])
        opening = [r for r in rows if r.kind == "open" and r.spread_home == r.spread_home]
        polls = [r for r in rows if r.kind == "poll" and r.spread_home == r.spread_home]
        now = polls[-1] if polls else (opening[-1] if opening else None)
        if now is None:
            return {"m_mkt_line": 0.0, "m_mkt_has": 0.0, "m_mkt_move": 0.0, "t_mkt_total": 0.0,
                    "t_mkt_has": 0.0}  # fmt: skip
        move = (now.spread_home - opening[-1].spread_home) if polls and opening else 0.0
        tot = now.total if now.total == now.total and now.total else 0.0
        return {"m_mkt_line": -now.spread_home, "m_mkt_has": 1.0, "m_mkt_move": -move,
                "t_mkt_total": tot, "t_mkt_has": float(tot > 0)}  # fmt: skip


GROUPS = {grp.name: grp for grp in (Base(), Availability(), Strength(), Rest(), Pace(),
                                    HomeCourt(), Context(), Market())}  # fmt: skip
ORDER = ["avail", "str", "rest", "pace", "hca", "ctx", "mkt"]
