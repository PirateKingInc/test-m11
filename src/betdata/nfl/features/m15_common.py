"""Shared by the M15 side-variable groups: recency-weighted team rates, regressed to the league.

For a rate num/den (e.g. pressures per dropback), every known team-game in the last
LOOKBACK_DAYS gets weight 0.5 ** (age / HALF_LIFE_DAYS), age measured back from the latest known
game (M12 strength's settings, so the rates depend only on what is known). A team's rate is
(sum w num + prior x league) / (sum w den + prior) minus the league rate: a team with little
data sits near the league mean. Offense rates group the team's own rows; defense ("allowed")
rates group the rows of the teams it played. Between seasons a rate keeps OFFSEASON_KEEP.
"""

from __future__ import annotations

import pandas as pd

from betdata.nfl.features.strength import HALF_LIFE_DAYS, LOOKBACK_DAYS, OFFSEASON_KEEP
from betdata.nfl.store import AsOf

__all__ = ["HALF_LIFE_DAYS", "team_rates", "season_keep", "matchup", "p_out"]


def weighted(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.assign(w=[])
    latest = df["kickoff"].max()
    df = df[df["kickoff"] >= latest - pd.Timedelta(days=LOOKBACK_DAYS)]
    age = (latest - df["kickoff"]).dt.total_seconds() / 86400
    return df.assign(w=0.5 ** (age / HALF_LIFE_DAYS))


def team_rates(df: pd.DataFrame, metrics: dict[str, tuple[str, str, float, str]],
               centre: bool = True) -> dict:  # fmt: skip
    """metrics: name -> (numerator, denominator, prior in denominator units, "team" or "opp").
    Returns {team: {name: rate - league}} (raw rate if centre is False)."""
    out: dict[int, dict[str, float]] = {}
    d = weighted(df)
    if d.empty:
        return out
    for name, (num, den, prior, by) in metrics.items():
        wn, wd = d[num] * d.w, d[den] * d.w
        league = float(wn.sum() / wd.sum()) if wd.sum() > 0 else 0.0
        sn = wn.groupby(d[by]).sum()
        sd = wd.groupby(d[by]).sum()
        rate = (sn + prior * league) / (sd + prior)
        for team, v in (rate - league if centre else rate).items():
            out.setdefault(team, {})[name] = float(v)
    return out


def season_keep(a: AsOf, frame: str, season: int) -> float:
    df = a.df(frame)
    latest = int(df["season"].max()) if len(df) else season
    return OFFSEASON_KEEP if season > latest else 1.0


def matchup(prefix: str, h: dict, w: dict, pairs: dict[str, tuple[str, str | None]],
            keep: float = 1.0) -> dict[str, float]:  # fmt: skip
    """pairs: feature -> (own metric, opponent metric or None). Home side = own(h) + opp(w);
    margin feature = home side - away side, total feature = their sum."""
    out = {}
    for f, (own, opp) in pairs.items():
        eh = h.get(own, 0.0) + (w.get(opp, 0.0) if opp else 0.0)
        ea = w.get(own, 0.0) + (h.get(opp, 0.0) if opp else 0.0)
        out[f"m_{prefix}_{f}"] = keep * (eh - ea)
        out[f"t_{prefix}_{f}"] = keep * (eh + ea)
    return out


def p_out(params: dict, bucket: str) -> float:
    """1 - M12's play probability, for a player the injury data flags: on this week's report
    (any status), inactive at prekick, or on a reserve list. Anyone else counts as playing."""
    from betdata.nfl.features.availability import play_prob

    parts = bucket.split("|")
    flagged = bucket in ("pk_out", "fr_reserve") or (len(parts) > 1 and parts[1] != "-")
    return 1.0 - play_prob(params, bucket) if flagged else 0.0


class Variant:
    """A targeted adjustment of a group for `nfl select15`: the same group (same column
    prefix), cached under its own version, keeping only the features named in `keep` (suffix
    after m_<group>_), or summing them into one feature `<group>_sum` when `combine` is set."""

    def __init__(self, base, version: int, keep: tuple[str, ...] | None = None,
                 combine: bool = False):  # fmt: skip
        self.base, self.name, self.version = base, base.name, version
        self.keep, self.combine = keep, combine

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        out = self.base.compute(a, g, point)
        n = len(self.name) + 3
        if self.keep is not None:
            out = {k: v for k, v in out.items() if k[n:] in self.keep}
        if self.combine:
            out = {f"{s}_{self.name}_sum": sum(v for k, v in out.items() if k[0] == s)
                   for s in ("m", "t")}  # fmt: skip
        return out
