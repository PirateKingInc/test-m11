"""CollegeFootballData.com (football) and CollegeBasketballData.com (basketball).

One API key and one shared quota (1,000 calls/month) cover both; both clients log usage
under the source name "cfbd" so the QuotaManager sees a single budget.

Jobs (calls each):
  teams_football / teams_basketball      1   team registry and aliases (run once a season)
  games_football / lines_football        1   whole season (schedule, scores / all providers)
  games_basketball / lines_basketball    1   a date window (default: 3 days back, 8 ahead)
CFBD is the schedule authority for NCAAF and NCAAB. Games are matched by CFBD team id; games
involving a team we don't track (non-D-I basketball opponents) are skipped, not queued.
Field names follow the official generated clients (cfbd 5.x, cbbd 1.x).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import select

from betdata.config import SourceConfig
from betdata.core.errors import ConfigError, ParseError
from betdata.core.http import CostReport, SourceClient
from betdata.core.matching import season_for, sport_id
from betdata.core.models import ParsedOdds
from betdata.core.odds_loader import load_odds
from betdata.core.quota import QuotaManager
from betdata.core.results_loader import load_results
from betdata.core.source import JobSpec, RawInput, Source
from betdata.core.teams_admin import add_alias, link_source_team, upsert_team
from betdata.core.venues import upsert_venue
from betdata.db.schema import raw_responses, team_source_ids
from betdata.db.schema import teams as teams_table
from betdata.sources import cfbd_games

NAME = "cfbd"
log = logging.getLogger(__name__)
FOOTBALL_BASE = "https://api.collegefootballdata.com"
BASKETBALL_BASE = "https://api.collegebasketballdata.com"
# The Odds API's NCAAF board is FBS plus FCS opponents; D-II/III would only add name clashes.
FOOTBALL_CLASSIFICATIONS = {"fbs", "fcs"}


class _Venue(BaseModel):
    id: int | None = None
    name: str | None = None
    city: str | None = None
    state: str | None = None
    countryCode: str | None = None
    timezone: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    elevation: str | float | None = None
    grass: bool | None = None
    dome: bool | None = None

    def fields(self) -> dict:
        try:
            elevation = float(self.elevation) if self.elevation not in (None, "") else None
        except ValueError:
            elevation = None
        return dict(
            name=self.name,
            city=self.city,
            state=self.state,
            country=self.countryCode,
            tz=self.timezone,
            lat=self.latitude,
            lon=self.longitude,
            elevation_m=elevation,
            roof_type=None if self.dome is None else ("dome" if self.dome else "open"),
            surface=None if self.grass is None else ("grass" if self.grass else "artificial"),
        )


class _FootballTeam(BaseModel):
    id: int
    school: str
    mascot: str | None = None
    abbreviation: str | None = None
    alternateNames: list[str] | None = None
    conference: str | None = None
    division: str | None = None
    classification: str | None = None
    location: _Venue | None = None


class _BasketballTeam(BaseModel):
    id: int
    sourceId: str | None = None  # ESPN team id
    school: str
    mascot: str | None = None
    abbreviation: str | None = None
    displayName: str | None = None
    shortDisplayName: str | None = None
    conference: str | None = None


@dataclass
class ParsedTeam:
    source_team_id: str
    canonical_name: str
    abbrev: str | None = None
    conference: str | None = None
    division: str | None = None
    division_level: str | None = None
    shared_aliases: set[str] = field(default_factory=set)  # names any source might use
    espn_id: str | None = None
    home_venue: _Venue | None = None


@dataclass
class ParsedTeams:
    sport: str
    teams: list[ParsedTeam]


def _validate(adapter: TypeAdapter, body: bytes, what: str):
    try:
        return adapter.validate_json(body)
    except ValidationError as e:
        raise ParseError(f"{NAME}: unexpected {what} response: {e}") from e


def parse_football_teams(body: bytes) -> ParsedTeams:
    out = []
    for t in _validate(TypeAdapter(list[_FootballTeam]), body, "football /teams"):
        level = (t.classification or "").lower()
        if level not in FOOTBALL_CLASSIFICATIONS:
            continue
        names = {n for n in (t.alternateNames or []) if n}
        if t.mascot:
            names.add(f"{t.school} {t.mascot}")
        out.append(
            ParsedTeam(
                source_team_id=str(t.id),
                canonical_name=t.school,
                abbrev=t.abbreviation,
                conference=t.conference,
                division=t.division,
                division_level=level.upper(),
                shared_aliases=names,
                home_venue=t.location if t.location and t.location.id is not None else None,
            )
        )
    return ParsedTeams("ncaaf", out)


def parse_basketball_teams(body: bytes) -> ParsedTeams:
    out = []
    for t in _validate(TypeAdapter(list[_BasketballTeam]), body, "basketball /teams"):
        names = {n for n in (t.displayName, t.shortDisplayName) if n}
        if t.mascot:
            names.add(f"{t.school} {t.mascot}")
        out.append(
            ParsedTeam(
                source_team_id=str(t.id),
                canonical_name=t.school,
                abbrev=t.abbreviation,
                conference=t.conference,
                division_level="D1",
                shared_aliases=names,
                espn_id=t.sourceId,
            )
        )
    return ParsedTeams("ncaab", out)


@dataclass
class TeamLoadStats:
    teams: int = 0
    aliases_added: int = 0
    aliases_skipped_ambiguous: list[str] = field(default_factory=list)


def load_teams(conn, batch: ParsedTeams) -> TeamLoadStats:
    stats = TeamLoadStats(teams=len(batch.teams))
    sid = sport_id(conn, batch.sport)
    canonical = {t.canonical_name for t in batch.teams} | set(
        conn.execute(
            teams_table.select().with_only_columns(teams_table.c.canonical_name)
            .where(teams_table.c.sport_id == sid)
        ).scalars()
    )  # fmt: skip

    # An alias claimed by two schools, or equal to another school's name, is never added.
    claims: dict[str, set[str]] = defaultdict(set)
    for t in batch.teams:
        for a in t.shared_aliases:
            claims[a].add(t.canonical_name)

    for t in batch.teams:
        team_id = upsert_team(
            conn,
            sport_id=sid,
            canonical_name=t.canonical_name,
            abbrev=t.abbrev,
            conference=t.conference,
            division=t.division,
            division_level=t.division_level,
        )
        link_source_team(
            conn, source=NAME, sport_id=sid, source_team_id=t.source_team_id, team_id=team_id
        )
        if t.espn_id:
            link_source_team(
                conn, source="espn", sport_id=sid, source_team_id=t.espn_id, team_id=team_id
            )
        if t.home_venue is not None:
            upsert_venue(conn, source=NAME, source_venue_id=str(t.home_venue.id),
                         **t.home_venue.fields())  # fmt: skip
        for alias in sorted(t.shared_aliases):
            if alias == t.canonical_name:
                continue
            if len(claims[alias]) > 1 or alias in canonical:
                stats.aliases_skipped_ambiguous.append(alias)
                continue
            stats.aliases_added += add_alias(
                conn, sport=batch.sport, source="*", alias=alias, team_id=team_id, persist=False
            )
        if t.abbrev:  # abbreviations clash across sources, so keep them CFBD-specific
            stats.aliases_added += add_alias(
                conn, sport=batch.sport, source=NAME, alias=t.abbrev, team_id=team_id, persist=False
            )
    if stats.aliases_skipped_ambiguous:
        log.info("skipped ambiguous aliases: %s", sorted(set(stats.aliases_skipped_ambiguous)))
    return stats


def cost_from_headers(resp: httpx.Response) -> CostReport:
    """CFBD counts one call per request. If it reports remaining calls in a header, use it
    (header name not documented; checked defensively)."""
    for name in ("x-calllimit-remaining", "x-ratelimit-remaining"):
        if name in resp.headers:
            try:
                return CostReport(cost=1, remaining=int(resp.headers[name]))
            except ValueError:
                pass
    return CostReport(cost=1)


class CfbdSource(Source):
    name = NAME

    def __init__(self, engine, config: SourceConfig, *, transport=None, sleep=None, clock=None):
        super().__init__(engine, config, clock)
        self.quota = (
            QuotaManager(engine, NAME, config.quota, **({"clock": clock} if clock else {}))
            if config.quota
            else None
        )
        extra = {k: v for k, v in dict(sleep=sleep, clock=clock).items() if v is not None}
        auth = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
        common = dict(
            quota=self.quota,
            cost_parser=cost_from_headers,
            transport=transport,
            headers=auth,
            **extra,
        )
        self.football = SourceClient(
            engine, NAME, config.options.get("football_base_url", FOOTBALL_BASE),
            label="football:", **common,
        )  # fmt: skip
        self.basketball = SourceClient(
            engine, NAME, config.options.get("basketball_base_url", BASKETBALL_BASE),
            label="basketball:", **common,
        )  # fmt: skip

    def close(self) -> None:
        self.football.close()
        self.basketball.close()

    @classmethod
    def jobs(cls) -> dict[str, JobSpec]:
        return {
            "teams_football": JobSpec("teams_football", "Import FBS+FCS teams and aliases."),
            "teams_basketball": JobSpec("teams_basketball", "Import D-I teams and aliases."),
            "venues_football": JobSpec(
                "venues_football", "All football venues with coordinates (1 call)."
            ),
            "games_football": JobSpec(
                "games_football", "Season schedule and scores (FBS games). --param season=2026"
            ),
            "lines_football": JobSpec(
                "lines_football", "Season lines, all providers. --param season=2026"
            ),
            "games_basketball": JobSpec(
                "games_basketball",
                "Schedule, status and scores for a window. --param start=2026-11-01 end=...",
            ),
            "lines_basketball": JobSpec(
                "lines_basketball", "Lines for a window, all providers. Same params."
            ),
        }

    def estimate_cost(self, job: str, params: dict[str, Any]) -> int:
        return 1

    def _window(self, params) -> dict[str, str]:
        today = self.clock().date()
        start = (
            date.fromisoformat(params["start"])
            if params.get("start")
            else today - timedelta(days=3)
        )
        end = date.fromisoformat(params["end"]) if params.get("end") else today + timedelta(days=8)
        iso = lambda d, t: datetime.combine(d, t, tzinfo=UTC).isoformat().replace("+00:00", "Z")  # noqa: E731
        return {"startDateRange": iso(start, time.min), "endDateRange": iso(end, time(23, 59, 59))}

    def request_for(self, job: str, params: dict[str, Any]):
        """(client, path, query) for a job: shared by fetch and by backfill's cache check."""
        season = int(params["season"]) if params.get("season") else None
        if job == "teams_football":
            return self.football, "/teams", {"year": season or season_for("ncaaf", self.clock())}
        if job == "teams_basketball":
            return self.basketball, "/teams", ({"season": season} if season else {})
        if job == "venues_football":
            return self.football, "/venues", {}
        if job in ("games_football", "lines_football"):
            query = {"year": season or season_for("ncaaf", self.clock()), "seasonType": "both"}
            if job == "games_football":
                query["classification"] = "fbs"  # every game with an FBS team, incl. vs FCS
            return self.football, "/games" if job == "games_football" else "/lines", query
        path = "/games" if job == "games_basketball" else "/lines"
        return self.basketball, path, self._window(params)

    def already_fetched(self, job: str, params: dict[str, Any]) -> bool:
        """True if this exact request was answered successfully before (raw archive)."""
        client, path, query = self.request_for(job, params)
        with self.engine.connect() as conn:
            stored = conn.execute(
                select(raw_responses.c.params).where(
                    raw_responses.c.source == NAME,
                    raw_responses.c.endpoint == client.label + path,
                    raw_responses.c.http_status == 200,
                )
            ).scalars()
            return any(p == query for p in stored)

    def fetch(self, job: str, params: dict[str, Any], *, priority: str, run_id: int) -> RawInput:
        if not self.config.api_key:
            raise ConfigError(f"{self.config.api_key_env} is not set (see .env.example)")
        client, path, query = self.request_for(job, params)
        resp = client.get(path, query, est_cost=1, priority=priority, run_id=run_id, dedupe=True)
        return RawInput(client.label + path, query, resp.body, resp.fetched_at, resp.raw_id)

    def job_for_endpoint(self, endpoint: str) -> str:
        sport, path = endpoint.split(":", 1)
        return f"{path.strip('/')}_{sport}"  # football:/lines -> lines_football

    def parse(self, job: str, raw: RawInput):
        parsers = {
            "teams_football": lambda: parse_football_teams(raw.body),
            "teams_basketball": lambda: parse_basketball_teams(raw.body),
            "venues_football": lambda: _validate(TypeAdapter(list[_Venue]), raw.body, "/venues"),
            "games_football": lambda: cfbd_games.parse_football_games(raw.body, raw.fetched_at),
            "lines_football": lambda: cfbd_games.parse_football_lines(raw.body, raw.fetched_at),
            "games_basketball": lambda: cfbd_games.parse_basketball_games(raw.body, raw.fetched_at),
            "lines_basketball": lambda: cfbd_games.parse_basketball_lines(raw.body, raw.fetched_at),
        }
        return parsers[job]()

    def load(self, conn, job: str, batch, *, run_id, raw_id) -> tuple[int, int]:
        if job == "venues_football":
            for v in batch:
                if v.id is not None:
                    upsert_venue(conn, source=NAME, source_venue_id=str(v.id), **v.fields())
            return len(batch), len(batch)
        if job.startswith("teams_"):
            stats = load_teams(conn, batch)
            return stats.aliases_added, stats.teams
        batch = _only_known_teams(conn, batch)
        stats = load_odds(conn, source=NAME, batch=batch, run_id=run_id, raw_id=raw_id)
        changed = 0
        if job.startswith("games_"):
            changed = load_results(conn, source=NAME, events=batch.events, matches=stats.matches)
        return stats.inserted + changed, len(batch.events)


def _only_known_teams(conn, batch: ParsedOdds) -> ParsedOdds:
    """Drop games involving a team we don't track (by CFBD team id) and their prices."""
    known = set(
        conn.execute(
            select(team_source_ids.c.source_team_id).where(
                team_source_ids.c.source == NAME,
                team_source_ids.c.sport_id == sport_id(conn, batch.sport),
            )
        ).scalars()
    )
    if not known:
        raise ConfigError(
            f"no {batch.sport} teams imported yet; run `betdata teams import --sport {batch.sport}`"
        )
    keep = {
        e.source_game_id
        for e in batch.events
        if e.home_source_team_id in known and e.away_source_team_id in known
    }
    skipped = len(batch.events) - len(keep)
    if skipped:
        log.info("cfbd %s: skipped %d games involving untracked teams", batch.sport, skipped)
    batch.events = [e for e in batch.events if e.source_game_id in keep]
    batch.prices = [p for p in batch.prices if p.source_game_id in keep]
    return batch


def backfill_plan(sport: str, seasons: list[int]) -> list[tuple[str, dict[str, Any]]]:
    """Calls needed to load past seasons: 2 per football season; basketball goes month by month
    (a season is ~6,000 games), 12 per season."""
    plan: list[tuple[str, dict[str, Any]]] = []
    for season in sorted(seasons):
        if sport == "ncaaf":
            plan += [("games_football", {"season": season}), ("lines_football", {"season": season})]
            continue
        bounds = [date(season, 10, 25), date(season, 12, 1), date(season + 1, 1, 1),
                  date(season + 1, 2, 1), date(season + 1, 3, 1), date(season + 1, 4, 1),
                  date(season + 1, 4, 16)]  # fmt: skip
        for a, b in zip(bounds, bounds[1:], strict=False):
            window = {"start": a.isoformat(), "end": (b - timedelta(days=1)).isoformat()}
            plan += [("games_basketball", window), ("lines_basketball", window)]
    return plan
