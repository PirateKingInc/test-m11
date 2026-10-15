"""CollegeFootballData.com (football) and CollegeBasketballData.com (basketball).

One API key and one shared quota (1,000 calls/month) cover both; both clients log usage
under the source name "cfbd" so the QuotaManager sees a single budget.

M4 implements team import only (`/teams`, ~2 calls per season). Field names follow the
official generated clients (cfbd 5.x `Team`, cbbd 1.x `TeamInfo`).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from betdata.config import SourceConfig
from betdata.core.errors import ConfigError, ParseError
from betdata.core.http import CostReport, SourceClient
from betdata.core.matching import season_for, sport_id
from betdata.core.quota import QuotaManager
from betdata.core.source import JobSpec, RawInput, Source
from betdata.core.teams_admin import add_alias, link_source_team, upsert_team
from betdata.db.schema import teams as teams_table

NAME = "cfbd"
log = logging.getLogger(__name__)
FOOTBALL_BASE = "https://api.collegefootballdata.com"
BASKETBALL_BASE = "https://api.collegebasketballdata.com"
# The Odds API's NCAAF board is FBS plus FCS opponents; D-II/III would only add name clashes.
FOOTBALL_CLASSIFICATIONS = {"fbs", "fcs"}


class _FootballTeam(BaseModel):
    id: int
    school: str
    mascot: str | None = None
    abbreviation: str | None = None
    alternateNames: list[str] | None = None
    conference: str | None = None
    division: str | None = None
    classification: str | None = None


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
            "teams_football": JobSpec(
                "teams_football", "Import FBS+FCS teams and aliases (1 call)."
            ),
            "teams_basketball": JobSpec(
                "teams_basketball", "Import D-I basketball teams and aliases (1 call)."
            ),
        }

    def estimate_cost(self, job: str, params: dict[str, Any]) -> int:
        return 1

    def fetch(self, job: str, params: dict[str, Any], *, priority: str, run_id: int) -> RawInput:
        if not self.config.api_key:
            raise ConfigError(f"{self.config.api_key_env} is not set (see .env.example)")
        if job == "teams_football":
            client, endpoint = self.football, "/teams"
            query = {"year": params.get("season") or season_for("ncaaf", self.clock())}
        else:
            client, endpoint = self.basketball, "/teams"
            query = {"season": params["season"]} if params.get("season") else {}
        resp = client.get(endpoint, query, est_cost=1, priority=priority, run_id=run_id)
        return RawInput(client.label + endpoint, query, resp.body, resp.fetched_at, resp.raw_id)

    def job_for_endpoint(self, endpoint: str) -> str:
        return {"football:/teams": "teams_football", "basketball:/teams": "teams_basketball"}[
            endpoint
        ]

    def parse(self, job: str, raw: RawInput) -> ParsedTeams:
        if job == "teams_football":
            return parse_football_teams(raw.body)
        return parse_basketball_teams(raw.body)

    def load(self, conn, job: str, batch: ParsedTeams, *, run_id, raw_id) -> tuple[int, int]:
        stats = load_teams(conn, batch)
        return stats.aliases_added, stats.teams
