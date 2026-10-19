"""Base model: home field and the M10 Elo rating difference for the margin; the league's
recent scoring level for the total. Every M12 group has to improve on this."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from betdata.nfl.store import AsOf

# M10's Elo settings, chosen on 2000-2014 (README "Testing for an edge").
ELO = dict(k=20.0, hfa=60.0, revert=0.5, bye=25.0)
ELO_MEAN = 1505.0
ELO_PER_POINT = 25.0
LEAGUE_ALPHA = 0.01  # weight of each new game in the league scoring average
LEAGUE_START = 43.0


@dataclass
class EloState:
    """Elo ratings after the first `n` known results (in known_at order)."""

    n: int = 0
    season: int | None = None
    ratings: dict[int, float] = field(default_factory=dict)
    league_total: float = LEAGUE_START

    def rating(self, team: int, season: int) -> float:
        r = self.ratings.get(team, ELO_MEAN)
        if self.season is not None and season > self.season:
            r = ELO_MEAN + (r - ELO_MEAN) * (1 - ELO["revert"])
        return r

    def apply(self, g, home_score: int, away_score: int, rest: tuple[float, float]) -> None:
        if self.season is not None and g.season > self.season:
            self.ratings = {
                t: ELO_MEAN + (r - ELO_MEAN) * (1 - ELO["revert"]) for t, r in self.ratings.items()
            }
        self.season = g.season if self.season is None else max(self.season, g.season)
        d = diff(self, g, rest)
        expected = 1 / (1 + 10 ** (-d / 400))
        margin = home_score - away_score
        actual = 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
        if margin == 0:
            mult = 1.0
        else:
            winner_diff = d if margin > 0 else -d
            mult = math.log(abs(margin) + 1) * 2.2 / (winner_diff * 0.001 + 2.2)
        shift = ELO["k"] * mult * (actual - expected)
        self.ratings[g.home] = self.ratings.get(g.home, ELO_MEAN) + shift
        self.ratings[g.away] = self.ratings.get(g.away, ELO_MEAN) - shift
        self.league_total += LEAGUE_ALPHA * (home_score + away_score - self.league_total)


def diff(state: EloState, g, rest: tuple[float, float]) -> float:
    d = state.rating(g.home, g.season) - state.rating(g.away, g.season)
    if not g.neutral:
        d += ELO["hfa"]
    if rest[0] >= 13:
        d += ELO["bye"]
    if rest[1] >= 13:
        d -= ELO["bye"]
    return d


def elo_state(a: AsOf) -> EloState:
    """Elo after every result known at a.t. Advances incrementally from the last state this
    store computed when it can (results only ever append), else starts over."""
    n = a.n("results")
    st: EloState | None = a.store.cache.get("elo_state")
    if st is None or st.n > n:
        st = EloState()
    results = a.store.frame("results").df
    games = a.games
    for i in range(st.n, n):
        r = results.iloc[i]
        g = games.loc[r.game_id]
        st.apply(g, int(r.home_score), int(r.away_score), (g.home_rest, g.away_rest))
    st.n = n
    a.store.cache["elo_state"] = st
    return EloState(st.n, st.season, dict(st.ratings), st.league_total)


class Base:
    name = "base"

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        st = elo_state(a)
        return {
            "m_base_hfa": 0.0 if g.neutral else 1.0,
            "m_base_elo": diff(st, g, (g.home_rest, g.away_rest)) / ELO_PER_POINT
            - (0.0 if g.neutral else ELO["hfa"] / ELO_PER_POINT),
            "t_base_league": st.league_total,
        }
