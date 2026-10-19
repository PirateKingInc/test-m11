"""Look-ahead harness: a feature computed at instant t must be the same whether the store holds
all data, only the rows known at t, or all data with every later row scrambled."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from betdata.nfl.pipeline import game_features
from betdata.nfl.points import POINTS, point_time
from betdata.nfl.store import Store, frames_equal


@dataclass(frozen=True)
class Violation:
    game_id: int
    point: str
    variant: str  # truncated or scrambled
    features: tuple[str, ...]


def check(store: Store, groups, game_ids: Iterable[int], points: Sequence[str] = POINTS,
          seed: int = 0) -> list[Violation]:  # fmt: skip
    out = []
    for gid in game_ids:
        kickoff = store.games.at[gid, "kickoff"]
        for p in points:
            t = point_time(p, kickoff)
            full = game_features(store, groups, gid, p)
            for variant, other in (("truncated", store.truncated(t)),
                                   ("scrambled", store.scrambled(t, seed))):  # fmt: skip
                bad = frames_equal(full, game_features(other, groups, gid, p))
                if bad:
                    out.append(Violation(gid, p, variant, tuple(bad)))
    return out
