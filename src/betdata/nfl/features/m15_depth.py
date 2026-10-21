"""M15 group `depth` (#105): who actually replaces an injured starter.

For each position group except QB (M12 availability already values the backup QB): the latest
depth chart known at the point (weekly charts to 2024: week N counts from the week-N prekick;
daily snapshots from 2025) lists the starters (the first n players by rank, then slot: RB 1,
WR 3, TE 1, OL 5, DL 4, LB 3, DB 4). A starter's chance of missing the game is 1 - M12's play
probability, counted only when the injury data flags him (on the week's report, inactive at
prekick, or on a reserve list; otherwise he counts as playing). His replacement is the next
player behind him at his own slot who is likely to play, else the group's highest-ranked such
backup.

Player value: recency-weighted share of his unit's snaps over every game he logged (any team,
half-life as M12 strength), shrunk toward zero by VALUE_PRIOR games, i.e. how much of the
field the coaches have given him. Feature per team and group: sum over starters of
P(out) x (value of replacement - value of starter); margin = home - away, total = sum.
"""

from __future__ import annotations

from collections import defaultdict

from betdata.nfl.features.availability import candidates
from betdata.nfl.features.m15_common import HALF_LIFE_DAYS, Variant, p_out
from betdata.nfl.features.state import Rolling, advanced
from betdata.nfl.store import AsOf, to_ns

STARTERS = {"RB": 1, "WR": 3, "TE": 1, "OL": 5, "DL": 4, "LB": 3, "DB": 4}
SNAP_GROUP = {"CB": "DB", "S": "DB"}
VALUE_PRIOR = 2.0  # games at zero snaps
NS_PER_DAY = 86400 * 10**9


class PlayerValue(Rolling):
    frame = "snaps"

    def __init__(self):
        super().__init__()
        self.s: dict[str, float] = defaultdict(float)
        self.w: dict[str, float] = defaultdict(float)
        self.last: dict[str, int] = {}

    def step(self, r) -> None:
        t = to_ns(r.kickoff)
        if r.key in self.last:
            d = 0.5 ** (max(0, t - self.last[r.key]) / NS_PER_DAY / HALF_LIFE_DAYS)
            self.s[r.key] *= d
            self.w[r.key] *= d
        self.last[r.key] = max(t, self.last.get(r.key, t))
        self.s[r.key] += float(r.share)
        self.w[r.key] += 1.0

    def value(self, key, t_ns: int, prior: float = VALUE_PRIOR) -> float:
        if key not in self.last:
            return 0.0
        d = 0.5 ** (max(0, t_ns - self.last[key]) / NS_PER_DAY / HALF_LIFE_DAYS)
        return self.s[key] * d / (self.w[key] * d + prior)


class Charts(Rolling):
    """Each team's latest depth chart: rows sharing the newest known_at."""

    frame = "m15_depth"

    def __init__(self):
        super().__init__()
        self.at: dict[int, int] = {}
        self.rows: dict[int, list] = {}

    def step(self, r) -> None:
        t = to_ns(r.known_at)
        if r.team not in self.at or t > self.at[r.team]:
            self.at[r.team], self.rows[r.team] = t, []
        if r.group in STARTERS:
            self.rows[r.team].append((r.group, int(r.rank), str(r.slot), r.gsis_id))


def team_drops(a: AsOf, g, team: int, point: str,
               prior: float = VALUE_PRIOR) -> dict[str, float]:  # fmt: skip
    values = advanced(a, "m15_values", PlayerValue)
    charts = advanced(a, "m15_charts", Charts)
    params = a.store.params
    q_out = {k: p_out(params, b) for k, _, _, b in candidates(a, g, team, point)}
    out = dict.fromkeys(STARTERS, 0.0)
    rows = charts.rows.get(team, [])
    for grp, n in STARTERS.items():
        order, seen = [], set()
        for _, rank, slot, key in sorted(r for r in rows if r[0] == grp):
            if key not in seen:
                seen.add(key)
                order.append((rank, slot, key))
        starters = order[:n]
        names = {k for _, _, k in starters}
        bench = [x for x in order[n:] if q_out.get(x[2], 0.0) < 0.5]
        for rank, slot, key in starters:
            q = q_out.get(key, 0.0)
            if q <= 0.0:
                continue
            same = [x for x in bench if x[1] == slot and x[0] > rank and x[2] not in names]
            sub = (same or bench or [(0, "", None)])[0][2]
            v_sub = values.value(sub, a.t_ns, prior) if sub is not None else 0.0
            out[grp] += q * (v_sub - values.value(key, a.t_ns, prior))
    return out


class Depth:
    name = "depth"

    def __init__(self, version: int = 1, value_prior: float = VALUE_PRIOR):
        self.version, self.prior = version, value_prior

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        h = team_drops(a, g, g.home, point, self.prior)
        w = team_drops(a, g, g.away, point, self.prior)
        out = {}
        for k in STARTERS:
            out[f"m_depth_{k.lower()}"] = h[k] - w[k]
            out[f"t_depth_{k.lower()}"] = h[k] + w[k]
        return out


# Targeted adjustments tried by `nfl select15` (label, group); filled from validation runs.
ADJUSTMENTS = [
    ("1: one feature, summed over the groups", Variant(Depth(), 101, combine=True)),
    ("2: OL, WR and DB only (M12's cluster groups)",
     Variant(Depth(), 102, keep=("ol", "wr", "db"))),
    ("3: player value shrunk with 6 games instead of 2", Depth(103, value_prior=6.0)),
]
