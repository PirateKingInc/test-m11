"""As-of data store for the M12 NFL model.

Every time-varying input is a `Frame`: a DataFrame sorted by `known_at` (when the row became
known, per the rules in MODEL.md). `AsOf(store, t)` exposes only the prefix of each frame with
known_at <= t, so a feature can't read anything published after its prediction point. The
schedule itself (teams, kickoff, site, week) is known in advance and lives in `store.games`.

Derived values that are expensive (ratings, player values) are cached per store, keyed by the
number of rows known in the frames they read. Because frames only grow at the end, the same key
always means the same data; a truncated or scrambled copy of the store gets its own cache.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

FRAMES = (
    "results",  # game_id, home_score, away_score
    "team_games",  # game_id, team, opp, season, week, kickoff, home (bool), stats...
    "player_games",  # game_id, team, gsis, season, kickoff, dropbacks, dropback_epa, ...
    "snaps",  # game_id, team, key, group, share, season, week, kickoff
    "injuries",  # season, week, team, key, report_status, practice_status
    "rosters",  # season, week, team, key, status, group
    "coaches",  # game_id, home_coach, away_coach
    "gameday",  # game_id, roof_state, referee
    "odds",  # game_id, market (spreads/totals), line (home spread / total), source
    "weather",  # game_id, fetched_at, temp_c, wind_kph, precip_mm (forecasts only)
)


def to_ns(t: datetime | pd.Timestamp) -> int:
    return pd.Timestamp(t).value


@dataclass
class Frame:
    df: pd.DataFrame
    known: np.ndarray = field(init=False)

    def __post_init__(self):
        if "known_at" not in self.df.columns:
            raise ValueError("a Frame needs a known_at column")
        self.df = self.df.sort_values("known_at", kind="stable").reset_index(drop=True)
        self.known = self.df["known_at"].map(to_ns).to_numpy(dtype="int64") if len(
            self.df
        ) else np.array([], dtype="int64")

    def count(self, t_ns: int) -> int:
        return int(np.searchsorted(self.known, t_ns, side="right"))

    def upto(self, t_ns: int) -> pd.DataFrame:
        return self.df.iloc[: self.count(t_ns)]


def empty_frame(columns: list[str]) -> Frame:
    return Frame(pd.DataFrame({c: [] for c in [*columns, "known_at"]}))


@dataclass
class Store:
    """games: one row per game (schedule facts only, known in advance), indexed by game_id."""

    games: pd.DataFrame
    frames: dict[str, Frame]
    params: dict[str, Any] = field(default_factory=dict)  # fitted on training seasons only
    cache: dict = field(default_factory=dict, repr=False)

    def frame(self, name: str) -> Frame:
        return self.frames[name]

    def truncated(self, t: datetime) -> Store:
        """Only the rows known at t: what a predictor at t could possibly have."""
        t_ns = to_ns(t)
        return Store(
            self.games,
            {k: Frame(f.upto(t_ns).copy()) for k, f in self.frames.items()},
            self.params,
        )

    def scrambled(self, t: datetime, seed: int = 0) -> Store:
        """Every row known after t has its numbers and labels shuffled or replaced, and fake
        future rows are added. A feature at t must not notice."""
        rng = np.random.default_rng(seed)
        t_ns = to_ns(t)
        out = {}
        for name, f in self.frames.items():
            n = f.count(t_ns)
            before, after = f.df.iloc[:n], f.df.iloc[n:].copy()
            if len(after):
                for c in after.columns:
                    if c in ("known_at",):
                        continue
                    col = after[c]
                    if pd.api.types.is_bool_dtype(col):
                        after[c] = rng.random(len(after)) < 0.5
                    elif pd.api.types.is_numeric_dtype(col) and c not in ("game_id",):
                        after[c] = rng.normal(0, 50, len(after)).astype(col.dtype)
                    else:
                        after[c] = rng.permutation(col.to_numpy())
                extra = after.sample(n=min(len(after), 25), random_state=seed)
                after = pd.concat([after, extra], ignore_index=True)
            out[name] = Frame(pd.concat([before, after], ignore_index=True))
        return Store(self.games, out, self.params)

    def with_params(self, **params) -> Store:
        return Store(self.games, self.frames, {**self.params, **params})


class AsOf:
    """The store as seen at instant t."""

    def __init__(self, store: Store, t: datetime | pd.Timestamp):
        self.store = store
        self.t = pd.Timestamp(t)
        self.t_ns = to_ns(t)

    @property
    def games(self) -> pd.DataFrame:
        return self.store.games

    def df(self, name: str) -> pd.DataFrame:
        return self.store.frames[name].upto(self.t_ns)

    def n(self, name: str) -> int:
        return self.store.frames[name].count(self.t_ns)

    def cached(self, key: Any, frames: tuple[str, ...], fn: Callable[[], Any]) -> Any:
        """fn() depends only on the known prefix of `frames`; computed once per prefix."""
        k = (key, tuple(self.n(f) for f in frames))
        if k not in self.store.cache:
            self.store.cache[k] = fn()
        return self.store.cache[k]


def frames_equal(a: dict[str, float], b: dict[str, float]) -> list[str]:
    """Names of features whose values differ (NaN equals NaN)."""
    bad = []
    for k in sorted(set(a) | set(b)):
        x, y = a.get(k), b.get(k)
        if x is None or y is None:
            bad.append(k)
        elif not (x == y or (np.isnan(x) and np.isnan(y)) or abs(x - y) < 1e-9):
            bad.append(k)
    return bad
