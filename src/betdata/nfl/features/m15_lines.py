"""M15 group `lines` (#104): offensive line vs defensive front.

Pass: pressure rate allowed, sack rate and time to throw (offense) against pressure rate and
sack rate generated (defense); the home QB's expected pressure is home offense + away defense.
Pressure per dropback comes from participation (2016-2025), else PFR weekly pressures (2018+),
else pbp sacks + QB hits, the fallbacks scaled to participation on training games
(nfl/m15data.py), so every game has a value. Run: EPA and success per designed carry, offense
against the defense's allowed rates. All recency-weighted and regressed to the league
(m15_common). OL continuity: of the team's last four games, the share of OL starts (top five OL
by snaps) made by the five who started the latest one. OL starters out: expected number of
OL starters (M12 role >= 0.6) who don't play: 1 - M12's play probability for each starter the
injury data flags (report at the final report; game-day inactives at prekick).
"""

from __future__ import annotations

from betdata.nfl.features.availability import STARTER, candidates, states
from betdata.nfl.features.m15_common import (
    Variant,
    matchup,
    p_out,
    season_keep,
    team_rates,
)
from betdata.nfl.store import AsOf

PRIOR = 150.0  # dropbacks / carries of league average
METRICS = {
    "off_press": ("press", "press_n", PRIOR, "team"),
    "def_press": ("press", "press_n", PRIOR, "opp"),
    "off_sack": ("sacks", "db", PRIOR, "team"), "def_sack": ("sacks", "db", PRIOR, "opp"),
    "off_ttt": ("ttt_sum", "ttt_n", PRIOR, "team"),
    "off_rush": ("rush_epa", "car", PRIOR, "team"), "def_rush": ("rush_epa", "car", PRIOR, "opp"),
    "off_rsucc": ("rush_succ", "car", PRIOR, "team"),
    "def_rsucc": ("rush_succ", "car", PRIOR, "opp"),
}  # fmt: skip
PAIRS = {"press": ("off_press", "def_press"), "sack": ("off_sack", "def_sack"),
         "ttt": ("off_ttt", None), "rush": ("off_rush", "def_rush"),
         "rsucc": ("off_rsucc", "def_rsucc")}  # fmt: skip
DEFAULT_CONTINUITY = 0.8


def ol_continuity(a: AsOf, team: int) -> float:
    roles = states(a)[0]
    games = roles.team.get(team, {})
    order = sorted(games, key=roles.kick.__getitem__)
    fives = []
    for gid in order:
        ol = sorted(((s, k) for k, (grp, s) in games[gid].items() if grp == "OL"), reverse=True)
        fives.append({k for _, k in ol[:5]})
    if len(fives) < 2 or not fives[-1]:
        return DEFAULT_CONTINUITY
    last = fives[-1]
    return sum(len(f & last) for f in fives[:-1]) / (len(last) * (len(fives) - 1))


def ol_out(a: AsOf, g, team: int, point: str) -> float:
    params = a.store.params
    return sum(p_out(params, b) for _, grp, role, b in candidates(a, g, team, point)
               if grp == "OL" and role >= STARTER)  # fmt: skip


class Lines:
    name = "lines"

    def __init__(self, version: int = 1, prior_scale: float = 1.0):
        self.version, self.scale = version, prior_scale
        self.metrics = {k: (n, d, p * prior_scale, by) for k, (n, d, p, by) in METRICS.items()}

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        rates = a.cached(("lines_rates", self.scale), ("m15_tg",),
                         lambda: team_rates(a.df("m15_tg"), self.metrics))  # fmt: skip
        keep = season_keep(a, "m15_tg", g.season)
        out = matchup("lines", rates.get(g.home, {}), rates.get(g.away, {}), PAIRS, keep)
        for f, fn in (("ol_cont", lambda t: ol_continuity(a, t)),
                      ("ol_out", lambda t: ol_out(a, g, t, point))):  # fmt: skip
            h, w = fn(g.home), fn(g.away)
            out[f"m_lines_{f}"], out[f"t_lines_{f}"] = h - w, h + w
        return out


# Targeted adjustments tried by `nfl select15` (label, group); filled from validation runs.
ADJUSTMENTS = [
    ("1: pass protection and OL only (rush terms overlap M12 strength's rush rating)",
     Variant(Lines(), 101, keep=("press", "sack", "ttt", "ol_cont", "ol_out"))),
    ("2: three times the prior (450 dropbacks / carries)", Lines(102, prior_scale=3.0)),
    ("3: pressure matchup and OL starters out only",
     Variant(Lines(), 103, keep=("press", "ol_out"))),
]
