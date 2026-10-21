"""Look-ahead harness for the NBA (M12's, extended): a feature at a prediction point must be the
same whether the store holds all data, only the rows known then, or all data with every later
row scrambled."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from betdata.nba.pipeline import game_features
from betdata.nba.points import POINTS, point_time
from betdata.nfl.asof import Violation
from betdata.nfl.store import Store, frames_equal


def check(store: Store, groups, game_ids: Iterable[int], points: Sequence[str] = POINTS,
          seed: int = 0) -> list[Violation]:  # fmt: skip
    out = []
    for gid in game_ids:
        tip = store.games.at[gid, "tip"]
        for p in points:
            t = point_time(p, tip)
            full = game_features(store, groups, gid, p)
            for variant, other in (("truncated", store.truncated(t)),
                                   ("scrambled", store.scrambled(t, seed))):  # fmt: skip
                bad = frames_equal(full, game_features(other, groups, gid, p))
                if bad:
                    out.append(Violation(gid, p, variant, tuple(bad)))
    return out
