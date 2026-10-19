"""Group 6: coaching tendencies of the head coach named for the game (known by the final
report), from every earlier game he coached, any team: fourth-down go rate in 4th-and-1-to-3
near midfield, and early-down neutral-situation pass rate over expected (nflfastR's pass_oe),
each shrunk toward the league."""

from __future__ import annotations

import pandas as pd

from betdata.nfl.store import AsOf

GO_SHRINK = 15.0  # fourth-down chances of league-average prior
PASS_SHRINK = 150.0  # early-down neutral plays of prior


def _coach_rates(coaches: pd.DataFrame, tg: pd.DataFrame) -> dict[str, dict]:
    if coaches.empty or tg.empty:
        return {}
    c = coaches.set_index("game_id")
    d = tg[["game_id", "home", "fourth_opps", "fourth_goes", "early_neutral_n",
            "early_neutral_pass_oe"]].join(c, on="game_id", how="inner")  # fmt: skip
    d["coach"] = d.home_coach.where(d.home, d.away_coach)
    go = d.fourth_goes.sum() / max(d.fourth_opps.sum(), 1.0)
    oe = d.early_neutral_pass_oe.sum() / max(d.early_neutral_n.sum(), 1.0)
    out = {}
    for coach, x in d.groupby("coach"):
        opps, n = x.fourth_opps.sum(), x.early_neutral_n.sum()
        out[coach] = {
            "go": (x.fourth_goes.sum() + GO_SHRINK * go) / (opps + GO_SHRINK) - go,
            "pass_oe": (x.early_neutral_pass_oe.sum() + PASS_SHRINK * oe) / (n + PASS_SHRINK) - oe,
        }
    return out


class Coaching:
    name = "coach"
    version = 1

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        coaches = a.df("coaches")
        rates = a.cached("coach_rates", ("coaches", "team_games"),
                         lambda: _coach_rates(coaches, a.df("team_games")))  # fmt: skip
        this = coaches[coaches.game_id == g.game_id]
        hc = this.home_coach.iloc[-1] if len(this) else None
        ac = this.away_coach.iloc[-1] if len(this) else None
        h, w = rates.get(hc, {}), rates.get(ac, {})
        out = {}
        for k in ("go", "pass_oe"):
            out[f"m_coach_{k}"] = h.get(k, 0.0) - w.get(k, 0.0)
            out[f"t_coach_{k}"] = h.get(k, 0.0) + w.get(k, 0.0)
        return out
