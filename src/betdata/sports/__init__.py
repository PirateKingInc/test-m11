"""Sports behind one small interface, so the hourly runner, grading and the page never name a
sport. A sport is a config entry (config/sports.yaml) plus, once it is active, a plugin class:

  key, name     "nfl", "NFL"
  jobs          its data refresh, in the scheduler's job format (cron in US Eastern on the
                hour; `when` gates on its games), run by the hourly runner when they are due
  predict()     store predictions for its upcoming games (no refitting: the plugin decides
                when its model is fitted)
  cards()       what the page shows per game, from stored predictions and quotes only
  grade()       grade finished games; records() the season record per market and version

"coming_soon" entries get a tab on the page and nothing else.
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Connection, Engine

from betdata.config import PROJECT_ROOT
from betdata.quotes import Quote
from betdata.scheduler.jobs import Gate, JobDef

SPORTS_PATH = PROJECT_ROOT / "config" / "sports.yaml"
ACTIVE, COMING_SOON = "active", "coming_soon"


@dataclass
class Card:
    """One game as the page shows it. Spreads are the home side (negative = home favoured);
    edges are from the home side's point of view (+ = the model likes home more than the
    market does): points on the spread, % points of win probability on the moneyline."""

    game_id: int
    kickoff: datetime
    home: str
    away: str
    point: str  # prediction point label, e.g. final_report, prekick, early
    predicted_at: datetime | None
    model_spread_home: float | None = None
    market_spread: Quote | None = None
    spread_edge: float | None = None
    spread_pick: str | None = None  # team, when the edge clears the validated threshold
    model_win_home: float | None = None
    ml_home: Quote | None = None
    ml_away: Quote | None = None
    market_win_home: float | None = None  # no-vig
    ml_edge: float | None = None
    ml_pick: str | None = None
    factors: list[tuple[str, float]] = field(default_factory=list)  # points toward home
    missing: dict[str, list[str]] = field(default_factory=dict)  # team -> players likely out
    notes: list[str] = field(default_factory=list)  # schedule flags, e.g. back-to-backs (M14)


@dataclass
class RecordRow:
    """Season record of one market for one model version."""

    market: str  # spread, moneyline, ...
    model_version: str
    wins: int
    losses: int
    pushes: int
    voids: int
    profit: float
    avg_clv: float | None
    beat_close: float | None
    clv_unit: str  # "pts" or "% pts"

    @property
    def bets(self) -> int:
        return self.wins + self.losses + self.pushes

    @property
    def roi(self) -> float | None:
        return self.profit / self.bets if self.bets else None


@dataclass
class SportEntry:
    key: str
    name: str
    status: str
    plugin: str | None = None
    jobs: list[JobDef] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)


class Sport(ABC):
    """An active sport's plugin."""

    def __init__(self, entry: SportEntry):
        self.entry = entry

    @property
    def key(self) -> str:
        return self.entry.key

    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def jobs(self) -> list[JobDef]:
        return self.entry.jobs

    @abstractmethod
    def predict(self, engine: Engine, now: datetime) -> int:
        """Store predictions for upcoming games; returns how many games were predicted."""

    @abstractmethod
    def cards(self, conn: Connection, now: datetime) -> list[Card]:
        """Upcoming games as of `now`, from stored data only (also used for past run times)."""

    @abstractmethod
    def grade(self, conn: Connection, now: datetime) -> dict[str, int]:
        """Grade finished games; returns counts (inserted, updated, pending)."""

    @abstractmethod
    def records(self, conn: Connection) -> list[RecordRow]:
        """Graded record per market and model version (markets shown on the page only)."""

    def thresholds(self) -> dict[str, str]:
        """Bet thresholds, for the page's legend."""
        return {}

    def moneyline_final_roi(self) -> float | None:
        """The final test's moneyline ROI (None: not scored). The page flags moneyline bets only
        when it is positive; otherwise moneyline edges are information only."""
        return None

    def bootstrap(self, engine: Engine, now: datetime,  # noqa: B027
                  source_kwargs: dict | None = None) -> None:  # fmt: skip
        """Rebuild this sport's history from free sources into an empty database."""


def _jobs(sport: str, raw: list[dict], defaults: dict) -> list[JobDef]:
    out = []
    for item in raw or []:
        body = {**defaults, **item}
        when = body.pop("when", None)
        months = body.pop("active_months", None)
        out.append(JobDef(**body, when=Gate(sport=sport, **when) if when else None,
                          active_months=tuple(months) if months else None))  # fmt: skip
    return out


def load_entries(path: Path | None = None) -> list[SportEntry]:
    raw = yaml.safe_load((path or SPORTS_PATH).read_text()) or {}
    defaults = raw.get("job_defaults", {})
    out = []
    for s in raw.get("sports", []):
        status = s.get("status", COMING_SOON)
        if status not in (ACTIVE, COMING_SOON):
            raise ValueError(f"sport {s['key']!r}: unknown status {status!r}")
        if status == ACTIVE and not s.get("plugin"):
            raise ValueError(f"sport {s['key']!r} is active but names no plugin")
        out.append(SportEntry(s["key"], s.get("name", s["key"].upper()), status,
                              s.get("plugin"), _jobs(s["key"], s.get("jobs"), defaults),
                              s.get("options", {})))  # fmt: skip
    return out


def plugin(entry: SportEntry) -> Sport:
    module, _, cls = entry.plugin.partition(":")
    return getattr(importlib.import_module(module), cls)(entry)


def active_sports(entries: list[SportEntry] | None = None) -> list[Sport]:
    return [plugin(e) for e in (entries or load_entries()) if e.status == ACTIVE]
