"""M15 group `pen` (#108): penalty discipline. Accepted penalties (pbp `penalty_team`, yards >
0) and penalty yards per scrimmage snap, on the team's offense and on its defense,
recency-weighted and regressed with 300 snaps of league average (m15_common)."""

from __future__ import annotations

from betdata.nfl.features.m15_common import matchup, season_keep, team_rates
from betdata.nfl.store import AsOf

PRIOR = 300.0
METRICS = {"off": ("off_pen", "off_plays", PRIOR, "team"),
           "off_yds": ("off_pen_yds", "off_plays", PRIOR, "team"),
           "def": ("def_pen", "def_plays", PRIOR, "team"),
           "def_yds": ("def_pen_yds", "def_plays", PRIOR, "team")}  # fmt: skip
PAIRS = {k: (k, None) for k in METRICS}


class Penalties:
    name = "pen"
    version = 1

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        r = a.cached("pen_rates", ("m15_tg",), lambda: team_rates(a.df("m15_tg"), METRICS))
        return matchup("pen", r.get(g.home, {}), r.get(g.away, {}), PAIRS,
                       season_keep(a, "m15_tg", g.season))  # fmt: skip


# Targeted adjustments tried by `nfl select15` (label, group); filled from validation runs.
ADJUSTMENTS: list = []
