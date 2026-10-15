"""The Odds API (v4) — https://the-odds-api.com/liveapi/guides/v4/

Free tier: 500 credits/month. /sports and /events cost nothing. /odds costs
(number of markets) x (number of regions); passing `bookmakers` instead of `regions` costs
one region per 10 books. A response with no events costs nothing. Usage headers on every
response (x-requests-remaining / -used / -last) are reconciled into the quota table.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from betdata.config import SourceConfig
from betdata.core.errors import ConfigError, ParseError
from betdata.core.http import CostReport, SourceClient
from betdata.core.models import ParsedEvent, ParsedOdds, ParsedPrice
from betdata.core.normalize.odds_math import american_to_decimal
from betdata.core.odds_loader import load_odds
from betdata.core.quota import QuotaManager
from betdata.core.source import JobSpec, RawInput, Source

NAME = "the_odds_api"
log = logging.getLogger(__name__)


# --- Response shapes --------------------------------------------------------


class _Outcome(BaseModel):
    name: str
    price: float
    point: float | None = None


class _Market(BaseModel):
    key: str
    last_update: datetime | None = None
    outcomes: list[_Outcome]


class _Bookmaker(BaseModel):
    key: str
    title: str | None = None
    last_update: datetime | None = None
    markets: list[_Market] = []


class _Event(BaseModel):
    id: str
    sport_key: str
    commence_time: datetime
    home_team: str | None = None
    away_team: str | None = None
    bookmakers: list[_Bookmaker] = []


_EVENTS = TypeAdapter(list[_Event])


def _int_header(headers: httpx.Headers, name: str) -> int | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def cost_from_headers(resp: httpx.Response) -> CostReport:
    return CostReport(
        cost=_int_header(resp.headers, "x-requests-last"),
        remaining=_int_header(resp.headers, "x-requests-remaining"),
        used=_int_header(resp.headers, "x-requests-used"),
    )


def estimate_cost(
    markets: list[str], regions: list[str] | None, bookmakers: list[str] | None
) -> int:
    region_units = math.ceil(len(bookmakers) / 10) if bookmakers else len(regions or [])
    return len(markets) * max(1, region_units)


# --- Parsing (pure) ---------------------------------------------------------


def _outcome_side(market: str, name: str, home: str, away: str) -> str | None:
    if market == "totals":
        return {"over": "over", "under": "under"}.get(name.lower())
    if name == home:
        return "home"
    if name == away:
        return "away"
    if market == "h2h" and name.lower() == "draw":
        return "draw"
    return None


def parse_odds(
    body: bytes,
    *,
    sport: str,
    fetched_at: datetime,
    requested_markets: tuple[str, ...] = (),
    requested_books: tuple[str, ...] | None = None,
) -> ParsedOdds:
    try:
        events = _EVENTS.validate_json(body)
    except ValidationError as e:
        raise ParseError(f"{NAME}: unexpected response shape: {e}") from e

    out = ParsedOdds(
        sport=sport,
        fetched_at=fetched_at,
        requested_markets=requested_markets,
        requested_books=requested_books,
    )
    for ev in events:
        if not ev.home_team or not ev.away_team:
            continue  # futures/outrights have no teams; not in scope
        out.events.append(ParsedEvent(ev.id, ev.commence_time, ev.home_team, ev.away_team))
        for bm in ev.bookmakers:
            for mk in bm.markets:
                for oc in mk.outcomes:
                    side = _outcome_side(mk.key, oc.name, ev.home_team, ev.away_team)
                    if side is None:
                        # Skip one odd selection rather than lose the whole snapshot; the raw
                        # response is kept, so it can be re-parsed once the mapping is fixed.
                        log.warning(
                            "%s: outcome %r in %s matches neither %r nor %r (event %s)",
                            NAME,
                            oc.name,
                            mk.key,
                            ev.home_team,
                            ev.away_team,
                            ev.id,
                        )
                        continue
                    american = round(oc.price)
                    out.prices.append(
                        ParsedPrice(
                            source_game_id=ev.id,
                            book_key=bm.key,
                            book_title=bm.title,
                            market_key=mk.key,
                            outcome=side,
                            line=oc.point if mk.key != "h2h" else None,
                            price_american=american,
                            price_decimal=american_to_decimal(american),
                            book_last_update=mk.last_update or bm.last_update,
                        )
                    )
    return out


# --- Source -----------------------------------------------------------------


class TheOddsApiSource(Source):
    name = NAME

    def __init__(
        self,
        engine,
        config: SourceConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep=None,
        clock=None,
    ):
        super().__init__(engine, config, clock)
        extra = {k: v for k, v in dict(sleep=sleep, clock=clock).items() if v is not None}
        quota_extra = {"clock": clock} if clock else {}
        self.quota = (
            QuotaManager(engine, NAME, config.quota, **quota_extra) if config.quota else None
        )
        self.client = SourceClient(
            engine,
            NAME,
            config.options.get("base_url", "https://api.the-odds-api.com/v4"),
            quota=self.quota,
            cost_parser=cost_from_headers,
            transport=transport,
            **extra,
        )

    @classmethod
    def jobs(cls) -> dict[str, JobSpec]:
        return {
            "events": JobSpec("events", "List upcoming events for a sport (free)."),
            "odds": JobSpec(
                "odds", "Snapshot featured markets for a sport (costs markets x regions)."
            ),
        }

    def _sport_key(self, sport: str) -> str:
        keys = self.config.options.get("sport_keys", {})
        if sport not in keys:
            raise KeyError(f"no sport_key configured for {sport!r}")
        return keys[sport]

    def _api_key(self) -> str:
        key = self.config.api_key
        if not key:
            raise ConfigError(f"{self.config.api_key_env} is not set (see .env.example)")
        return key

    def _odds_scope(self, params):
        markets = params.get("markets") or self.config.options.get("markets", ["h2h"])
        books = params.get("bookmakers")
        regions = None if books else self.config.options.get("regions", ["us"])
        return markets, regions, books

    def estimate_cost(self, job: str, params: dict[str, Any]) -> int:
        return estimate_cost(*self._odds_scope(params)) if job == "odds" else 0

    def fetch(self, job: str, params: dict[str, Any], *, priority: str, run_id: int) -> RawInput:
        sport = params.get("sport")
        if not sport:
            raise ConfigError(f"{NAME}: --sport is required")
        endpoint_base = f"/sports/{self._sport_key(sport)}"
        if job == "events":
            endpoint, query, cost = f"{endpoint_base}/events", {"dateFormat": "iso"}, 0
        else:
            markets, regions, books = self._odds_scope(params)
            endpoint = f"{endpoint_base}/odds"
            query = {
                "markets": ",".join(markets),
                "oddsFormat": self.config.options.get("odds_format", "american"),
                "dateFormat": "iso",
            }
            if books:
                query["bookmakers"] = ",".join(books)
            else:
                query["regions"] = ",".join(regions)
            cost = estimate_cost(markets, regions, books)
        query["apiKey"] = self._api_key()
        resp = self.client.get(endpoint, query, est_cost=cost, priority=priority, run_id=run_id)
        stored = {k: v for k, v in query.items() if k != "apiKey"}
        return RawInput(endpoint, stored, resp.body, resp.fetched_at, resp.raw_id)

    def job_for_endpoint(self, endpoint: str) -> str:
        return "events" if endpoint.endswith("/events") else "odds"

    def _sport_from_endpoint(self, endpoint: str) -> str:
        sport_key = endpoint.split("/")[2]
        for sport, key in self.config.options.get("sport_keys", {}).items():
            if key == sport_key:
                return sport
        raise ParseError(f"{NAME}: unknown sport key {sport_key!r}")

    def parse(self, job: str, raw: RawInput) -> ParsedOdds:
        params = raw.params or {}
        books = params.get("bookmakers")
        return parse_odds(
            raw.body,
            sport=self._sport_from_endpoint(raw.endpoint),
            fetched_at=raw.fetched_at,
            requested_markets=tuple(params["markets"].split(",")) if job == "odds" else (),
            requested_books=tuple(books.split(",")) if books else None,
        )

    def load(self, conn, job: str, batch: ParsedOdds, *, run_id, raw_id) -> tuple[int, int]:
        stats = load_odds(conn, source=NAME, batch=batch, run_id=run_id, raw_id=raw_id)
        return stats.inserted + stats.removed, stats.prices_seen or stats.events
