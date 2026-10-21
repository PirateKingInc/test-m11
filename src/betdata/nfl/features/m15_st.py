"""M15 group `st` (#106): special teams, from pbp special-teams plays.

Kicker: field goals made minus expected (a logistic of make probability by kick distance,
fitted on the training seasons; nfl/m15data.py) per attempt, over his whole recent career (any
team), regressed with FG_PRIOR attempts. A team's kicker is the one who kicked for it in its
latest known game. Punting: net EPA per punt for the punting team (the return included).
Kickoffs: EPA per kickoff for the kicking team (coverage). Returns: EPA per punt or kickoff
return for the receiving team. All recency-weighted and regressed (m15_common).
"""

from __future__ import annotations

from betdata.nfl.features.m15_common import matchup, season_keep, team_rates
from betdata.nfl.store import AsOf

FG_PRIOR = 20.0
METRICS = {"punt": ("punt_epa", "punt_n", 30.0, "team"), "ko": ("ko_epa", "ko_n", 30.0, "team"),
           "ret": ("ret_epa", "ret_n", 40.0, "team")}  # fmt: skip
PAIRS = {"fg": ("fg", None), "punt": ("punt", None), "ko": ("ko", None), "ret": ("ret", None)}


def kickers(kick):
    """({kicker: FG over expected per attempt, regressed}, {team: its latest kicker})."""
    if kick.empty:
        return {}, {}
    k = kick.assign(fg_oe=kick.fg_made - kick.fg_exp)
    rates = team_rates(k, {"fg": ("fg_oe", "fg_n", FG_PRIOR, "kicker")})
    latest = k.sort_values("kickoff", kind="stable").groupby("team")["kicker"].last()
    return {kk: v["fg"] for kk, v in rates.items()}, latest.to_dict()


class SpecialTeams:
    name = "st"
    version = 1

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        rates = a.cached("st_rates", ("m15_tg",), lambda: team_rates(a.df("m15_tg"), METRICS))
        fg, kicker = a.cached("st_kickers", ("m15_kick",), lambda: kickers(a.df("m15_kick")))
        h, w = dict(rates.get(g.home, {})), dict(rates.get(g.away, {}))
        h["fg"] = fg.get(kicker.get(g.home), 0.0)
        w["fg"] = fg.get(kicker.get(g.away), 0.0)
        return matchup("st", h, w, PAIRS, season_keep(a, "m15_tg", g.season))


# Targeted adjustments tried by `nfl select15` (label, group); filled from validation runs.
ADJUSTMENTS: list = []
