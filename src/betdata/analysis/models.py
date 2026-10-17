"""Game models for backtests and live predictions.

A model sees games strictly in time order: `predict(game)` before kickoff, then
`update(game)` once it is final. It never gets a game's result before predicting it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

from betdata.analysis.dataset import GameRow


@dataclass(frozen=True)
class Prediction:
    home_margin: float  # expected home points minus away points
    home_win_prob: float | None = None


class Model(Protocol):
    name: str

    def predict(self, game: GameRow) -> Prediction | None: ...

    def update(self, game: GameRow) -> None: ...

    def params(self) -> dict: ...


def version(model: Model) -> str:
    """Model name plus parameters, e.g. 'elo:bye=0,hfa=48,k=20,revert=0.33'."""
    params = ",".join(f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}"
                      for k, v in sorted(model.params().items()))  # fmt: skip
    return f"{model.name}:{params}" if params else model.name


@dataclass
class EloModel:
    """Team Elo ratings, after FiveThirtyEight's public NFL method.

    k: how far one result moves ratings. hfa: home-field advantage in Elo points (none at a
    neutral site). revert: share of each rating pulled back to the mean between seasons.
    bye: Elo bonus for a team coming off 13+ days of rest. 25 Elo points = 1 point of margin.
    The margin-of-victory multiplier grows with the winning margin and shrinks when the
    favourite wins, so blowouts by favourites don't inflate ratings.
    """

    k: float = 20.0
    hfa: float = 48.0
    revert: float = 0.33
    bye: float = 0.0
    name: str = "elo"
    mean: float = 1505.0
    elo_per_point: float = 25.0
    ratings: dict[int, float] = field(default_factory=dict)
    season: int | None = None

    def params(self) -> dict:
        return {"k": self.k, "hfa": self.hfa, "revert": self.revert, "bye": self.bye}

    def _roll(self, season: int) -> None:
        if self.season is not None and season != self.season:
            self.ratings = {
                t: self.mean + (r - self.mean) * (1 - self.revert) for t, r in self.ratings.items()
            }
        self.season = season

    def _diff(self, g: GameRow) -> float:
        """Home rating minus away rating, with home field and rest adjustments."""
        d = self.ratings.get(g.home_team_id, self.mean) - self.ratings.get(
            g.away_team_id, self.mean
        )
        if not g.neutral:
            d += self.hfa
        if g.home_rest_days is not None and g.home_rest_days >= 13:
            d += self.bye
        if g.away_rest_days is not None and g.away_rest_days >= 13:
            d -= self.bye
        return d

    def predict(self, g: GameRow) -> Prediction:
        self._roll(g.season)
        d = self._diff(g)
        return Prediction(d / self.elo_per_point, 1 / (1 + 10 ** (-d / 400)))

    def update(self, g: GameRow) -> None:
        if not g.final:
            return
        self._roll(g.season)
        d = self._diff(g)
        expected = 1 / (1 + 10 ** (-d / 400))
        margin = g.home_margin
        actual = 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
        if margin == 0:
            mult = 1.0
        else:
            winner_diff = d if margin > 0 else -d
            mult = math.log(abs(margin) + 1) * 2.2 / (winner_diff * 0.001 + 2.2)
        shift = self.k * mult * (actual - expected)
        self.ratings[g.home_team_id] = self.ratings.get(g.home_team_id, self.mean) + shift
        self.ratings[g.away_team_id] = self.ratings.get(g.away_team_id, self.mean) - shift


@dataclass
class MarketModel:
    """Predicts exactly the closing spread. A harness check: it must never find a bet, and
    its error is the market's own error."""

    name: str = "market"

    def params(self) -> dict:
        return {}

    def predict(self, g: GameRow) -> Prediction | None:
        return None if g.spread_home is None else Prediction(-g.spread_home)

    def update(self, g: GameRow) -> None:
        pass


MODELS = {"elo": EloModel, "market": MarketModel}
