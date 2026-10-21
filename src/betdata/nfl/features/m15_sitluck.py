"""M15 group `sitluck` (#107): situational luck, the part expected to revert.

Third downs: conversions minus expected (a logistic on log yards to go, fitted on training
seasons; nfl/m15data.py) per third down. Red zone: touchdowns minus expected (a logistic on the
yard line of the drive's first snap inside the 20) per trip. Offense (own plays) and defense
(opponents' plays against it). As M12's turnover luck (strength `int_off`/`int_def`): each is
the recency-weighted rate over expectation minus the league's, here also regressed with a
prior (100 third downs, 20 trips); the model learns how much of it reverts.
"""

from __future__ import annotations

from betdata.nfl.features.m15_common import matchup, season_keep, team_rates
from betdata.nfl.store import AsOf

METRICS = {"off3": ("oe3", "td3_n", 100.0, "team"), "def3": ("oe3", "td3_n", 100.0, "opp"),
           "offrz": ("oerz", "rz_n", 20.0, "team"), "defrz": ("oerz", "rz_n", 20.0, "opp")}
PAIRS = {k: (k, None) for k in METRICS}


def rates(tg):
    if len(tg):
        tg = tg.assign(oe3=tg.td3_conv - tg.td3_exp, oerz=tg.rz_td - tg.rz_exp)
    return team_rates(tg, METRICS)


class SituationalLuck:
    name = "sitluck"
    version = 1

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        r = a.cached("sitluck_rates", ("m15_tg",), lambda: rates(a.df("m15_tg")))
        return matchup("sitluck", r.get(g.home, {}), r.get(g.away, {}), PAIRS,
                       season_keep(a, "m15_tg", g.season))  # fmt: skip


# Targeted adjustments tried by `nfl select15` (label, group); filled from validation runs.
ADJUSTMENTS: list = []
