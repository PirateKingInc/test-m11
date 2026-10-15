"""SharpAPI — https://docs.sharpapi.io

Free tier: 12 requests/minute, DraftKings + FanDuel, odds delayed 60 s. We poll often near
tip/kickoff (the frequent-snapshot backbone, and the only free near-close lines for NBA/NCAAB),
but never faster than `min_interval_seconds` between requests.

Request/response shape follows the official SDK (sharpapi 0.4.x): `GET /api/v1/odds` with an
`X-API-Key` header, filters `league`, `sportsbook`, `market`, `live`, paged with
`limit` (max 500) / `offset`; body `{"data": [OddsLine...], "meta": {"pagination": ...}}`.
Rate-limit state comes back in `x-ratelimit-limit/remaining/reset` headers.

Each `OddsLine` is one selection: `event_id`, `home_team`, `away_team`, `event_start_time`,
`sportsbook`, `market_type`, `market_segment`, `team_side`, `selection`, `line`,
`odds_american`, and `is_active` (False = suspended with the price frozen; stored as
`suspended`, not dropped). Only full-game moneyline / spread / total are kept; props and
partial-game segments are skipped.

Unverified until a live call: the college league ids (`ncaab`, `ncaaf`) and whether the API
spells markets `point_spread`/`total_points` (SDK) or `spread`/`total` (one docs page) - both
are accepted when parsing, and the request filter is configurable in sources.yaml.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from betdata.config import SourceConfig
from betdata.core.errors import ConfigError, ParseError
from betdata.core.http import CostReport, SourceClient
from betdata.core.models import ParsedEvent, ParsedOdds, ParsedPrice
from betdata.core.normalize.odds_math import american_to_decimal
from betdata.core.odds_loader import load_odds
from betdata.core.quota import QuotaManager
from betdata.core.source import JobSpec, RawInput, Source

NAME = "sharpapi"
log = logging.getLogger(__name__)
MARKETS = {
    "moneyline": "h2h",
    "point_spread": "spreads",
    "spread": "spreads",
    "total_points": "totals",
    "total": "totals",
    "totals": "totals",
}
FULL_GAME = {None, "", "full_game", "game"}
PAGE_SIZE = 500
MAX_PAGES = 20  # runaway guard: 10,000 rows is far beyond any featured-market board


class _OddsLine(BaseModel):
    id: str | None = None
    sportsbook: str
    sportsbook_name: str | None = None
    event_id: str
    home_team: str
    away_team: str
    market_type: str
    market_segment: str | None = None
    selection: str | None = None
    selection_type: str | None = None
    team_side: str | None = None
    odds_american: float
    line: float | None = None
    event_start_time: datetime | None = None
    is_live: bool = False
    is_active: bool = True
    player_name: str | None = None


class _Page(BaseModel):
    data: list[dict[str, Any]]
    meta: dict[str, Any] | None = None


def cost_from_headers(resp: httpx.Response) -> CostReport:
    remaining = resp.headers.get("x-ratelimit-remaining")
    return CostReport(
        cost=1, remaining=int(remaining) if remaining and remaining.isdigit() else None
    )


def _outcome(line: _OddsLine, market: str) -> str | None:
    if market == "totals":
        text = (line.selection_type or line.selection or "").lower()
        if text.startswith("over"):
            return "over"
        if text.startswith("under"):
            return "under"
        return None
    side = (line.team_side or "").lower()
    if side in ("home", "away", "draw"):
        return side
    if line.selection == line.home_team:
        return "home"
    if line.selection == line.away_team:
        return "away"
    return None


def parse_odds(
    body: bytes, *, sport: str, fetched_at: datetime, complete: bool
) -> tuple[ParsedOdds, dict[str, int]]:
    """`complete=False` (a single page of several) disables `removed` detection."""
    try:
        page = _Page.model_validate_json(body)
    except ValidationError as e:
        raise ParseError(f"{NAME}: unexpected /odds response: {e}") from e

    out = ParsedOdds(
        sport=sport,
        fetched_at=fetched_at,
        requested_markets=("h2h", "spreads", "totals") if complete else (),
    )
    skipped = {"props_or_other_markets": 0, "partial_game": 0, "unmapped_side": 0, "no_start": 0}
    events: dict[str, ParsedEvent] = {}
    for item in page.data:
        try:
            line = _OddsLine.model_validate(item)
        except ValidationError as e:
            raise ParseError(f"{NAME}: unexpected odds line {item.get('id')}: {e}") from e
        market = MARKETS.get(line.market_type.lower())
        if market is None or line.player_name:
            skipped["props_or_other_markets"] += 1
            continue
        if line.market_segment not in FULL_GAME:
            skipped["partial_game"] += 1
            continue
        if line.event_start_time is None:
            skipped["no_start"] += 1
            continue
        outcome = _outcome(line, market)
        if outcome is None:
            skipped["unmapped_side"] += 1
            continue
        events.setdefault(
            line.event_id,
            ParsedEvent(line.event_id, line.event_start_time, line.home_team, line.away_team),
        )
        american = round(line.odds_american)
        out.prices.append(
            ParsedPrice(
                source_game_id=line.event_id,
                book_key=line.sportsbook,
                book_title=line.sportsbook_name,
                market_key=market,
                outcome=outcome,
                line=line.line if market != "h2h" else None,
                price_american=american,
                price_decimal=american_to_decimal(american) if abs(american) >= 100 else None,
                price_status="active" if line.is_active else "suspended",
            )
        )
    out.events = list(events.values())
    return out, skipped


class SharpApiSource(Source):
    name = NAME

    def __init__(self, engine, config: SourceConfig, *, transport=None, sleep=None, clock=None,
                 monotonic=None):  # fmt: skip
        super().__init__(engine, config, clock)
        self.quota = (
            QuotaManager(engine, NAME, config.quota, **({"clock": clock} if clock else {}))
            if config.quota
            else None
        )
        extra = {k: v for k, v in dict(sleep=sleep, clock=clock, monotonic=monotonic).items() if v}
        headers = {"X-API-Key": config.api_key} if config.api_key else {}
        self.client = SourceClient(
            engine,
            NAME,
            config.options.get("base_url", "https://api.sharpapi.io/api/v1"),
            quota=self.quota,
            cost_parser=cost_from_headers,
            transport=transport,
            headers=headers,
            min_interval=float(config.options.get("min_interval_seconds", 10)),
            **extra,
        )

    @classmethod
    def jobs(cls) -> dict[str, JobSpec]:
        return {
            "odds": JobSpec("odds", "Full-game ML/spread/total for a league (1 call per page).")
        }

    def estimate_cost(self, job: str, params: dict[str, Any]) -> int:
        return 1  # usually one page; extra pages are counted as they happen

    def _query(self, sport: str, offset: int) -> dict[str, Any]:
        leagues = self.config.options.get("leagues", {})
        if sport not in leagues:
            raise ConfigError(f"{NAME}: no league configured for {sport!r}")
        opts = self.config.options
        query = {
            "league": leagues[sport],
            "sportsbook": ",".join(opts.get("sportsbooks", ["draftkings", "fanduel"])),
            "limit": PAGE_SIZE,
            "offset": offset,
        }
        if opts.get("markets"):
            query["market"] = ",".join(opts["markets"])
        if opts.get("pregame_only", True):
            query["live"] = "false"
        return query

    def fetch(self, job: str, params: dict[str, Any], *, priority: str, run_id: int) -> RawInput:
        if not self.config.api_key:
            raise ConfigError(f"{self.config.api_key_env} is not set (see .env.example)")
        sport = params.get("sport")
        if not sport:
            raise ConfigError(f"{NAME}: --sport is required")
        rows: list[dict] = []
        offset, first, last = 0, None, None
        for _ in range(MAX_PAGES):
            resp = self.client.get(
                "/odds", self._query(sport, offset), est_cost=1, priority=priority, run_id=run_id
            )
            first = first or resp
            last = resp
            page = json.loads(resp.body)
            rows += page.get("data", [])
            pagination = (page.get("meta") or {}).get("pagination") or {}
            if not pagination.get("has_more"):
                break
            offset = pagination.get("next_offset") or offset + PAGE_SIZE
        else:
            log.warning("%s %s: stopped after %d pages", NAME, sport, MAX_PAGES)
        # Each page is archived as fetched; this combined body is what gets loaded. It is
        # marked complete so selections missing from the whole board can be flagged removed.
        body = json.dumps({"data": rows, "complete": True}).encode()
        return RawInput("/odds", {"sport": sport}, body, first.fetched_at, last.raw_id)

    def job_for_endpoint(self, endpoint: str) -> str:
        return "odds"

    def _sport_for_league(self, league: str) -> str:
        for sport, value in self.config.options.get("leagues", {}).items():
            if value == league:
                return sport
        raise ParseError(f"{NAME}: unknown league {league!r}")

    def parse(self, job: str, raw: RawInput) -> ParsedOdds:
        params = raw.params or {}
        if "sport" in params:  # combined board from a run
            sport, complete = params["sport"], json.loads(raw.body).get("complete", False)
        else:  # one archived page, replayed: never infer removals from a partial board
            sport, complete = self._sport_for_league(params.get("league", "")), False
        batch, skipped = parse_odds(
            raw.body, sport=sport, fetched_at=raw.fetched_at, complete=complete
        )
        if any(skipped.values()):
            log.info("%s %s skipped: %s", NAME, sport, {k: v for k, v in skipped.items() if v})
        return batch

    def load(self, conn, job: str, batch: ParsedOdds, *, run_id, raw_id) -> tuple[int, int]:
        stats = load_odds(conn, source=NAME, batch=batch, run_id=run_id, raw_id=raw_id)
        return stats.inserted + stats.removed, stats.prices_seen
