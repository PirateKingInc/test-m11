"""OddsPapi — https://oddspapi.io/en/docs

Free tier: ~250 requests/month (vendor's figure), hundreds of bookmakers including sharp ones
(Pinnacle). Its value here is a sharp reference line, so each call fetches one league's whole
board for one bookmaker:

    GET /v4/odds-by-tournaments?tournamentIds=<id>&bookmaker=<slug>&apiKey=<key>

Response (per vendor docs/tutorials): a list of fixtures with `fixtureId`, `startTime`,
participant names, and prices at
    bookmakerOdds.<slug>.markets.<marketId>.outcomes.<outcomeId>.players."0".price
Prices are decimal (the default odds format) and converted to American.

ARCHIVE-FIRST. This shape is documented only loosely and was not checked against a live
response, so nothing is guessed:
  * market/outcome ids mean nothing until mapped in sources.yaml (`oddspapi.markets`); fill
    the map from `betdata run oddspapi markets --param sport_id=11` (catalog, 1 call);
  * unmapped markets are counted and skipped, spreads/totals without a line are skipped;
  * every response is archived, so `betdata reparse oddspapi <raw_id>` rebuilds the history
    once the mapping is right;
  * a league without a tournament id is skipped (NotConfigured), not failed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from betdata.config import SourceConfig
from betdata.core.errors import ConfigError, NotConfigured, ParseError
from betdata.core.http import CostReport, SourceClient
from betdata.core.models import ParsedEvent, ParsedOdds, ParsedPrice
from betdata.core.normalize.odds_math import decimal_to_american
from betdata.core.odds_loader import load_odds
from betdata.core.quota import QuotaManager
from betdata.core.source import JobSpec, RawInput, Source

NAME = "oddspapi"
log = logging.getLogger(__name__)
SPREAD_TOTAL = {"spreads", "totals"}


def _line_of(market: dict, player: dict, mapping: dict) -> float | None:
    for value in (player.get("handicap"), player.get("line"), market.get("handicap"),
                  mapping.get("line")):  # fmt: skip
        if value not in (None, ""):
            return float(value)
    return None


def parse_board(
    body: bytes,
    *,
    sport: str,
    fetched_at: datetime,
    bookmaker: str,
    market_map: dict[str, dict],
    names: dict[str, str],
) -> tuple[ParsedOdds, dict[str, int]]:
    try:
        fixtures = json.loads(body)
    except ValueError as e:
        raise ParseError(f"{NAME}: response is not JSON: {e}") from e
    if isinstance(fixtures, dict):  # tolerate {"data": [...]} wrapping
        fixtures = fixtures.get("data", fixtures)
    if not isinstance(fixtures, list):
        raise ParseError(f"{NAME}: expected a list of fixtures, got {type(fixtures).__name__}")

    out = ParsedOdds(sport=sport, fetched_at=fetched_at)
    skipped: dict[str, int] = {"unmapped_market": 0, "unmapped_outcome": 0, "no_line": 0}
    unmapped_ids: set[str] = set()
    for fx in fixtures:
        try:
            fid = str(fx["fixtureId"])
            start = datetime.fromisoformat(str(fx["startTime"]).replace("Z", "+00:00"))
            home, away = fx[names["home"]], fx[names["away"]]
        except (KeyError, TypeError, ValueError) as e:
            raise ParseError(f"{NAME}: fixture missing expected field {e}; keys={list(fx)}") from e
        out.events.append(ParsedEvent(fid, start, str(home), str(away)))
        book = ((fx.get("bookmakerOdds") or {}).get(bookmaker)) or {}
        for market_id, market in (book.get("markets") or {}).items():
            mapping = market_map.get(str(market_id))
            if mapping is None:
                skipped["unmapped_market"] += 1
                unmapped_ids.add(str(market_id))
                continue
            for outcome_id, outcome in (market.get("outcomes") or {}).items():
                side = mapping.get("outcomes", {}).get(str(outcome_id))
                if side is None:
                    skipped["unmapped_outcome"] += 1
                    continue
                player = (outcome.get("players") or {}).get("0") or {}
                price = player.get("price")
                if price in (None, "") or float(price) <= 1:
                    continue
                line = _line_of(market, player, mapping)
                if mapping["market"] in SPREAD_TOTAL and line is None:
                    skipped["no_line"] += 1
                    continue
                decimal = float(price)
                out.prices.append(
                    ParsedPrice(
                        source_game_id=fid,
                        book_key=bookmaker,
                        book_title=bookmaker,
                        market_key=mapping["market"],
                        outcome=side,
                        line=line if mapping["market"] in SPREAD_TOTAL else None,
                        price_american=decimal_to_american(decimal),
                        price_decimal=round(decimal, 6),
                        price_status="suspended" if player.get("active") is False else "active",
                    )
                )
    if unmapped_ids:
        log.info("%s %s: unmapped market ids %s (see sources.yaml oddspapi.markets)",
                 NAME, sport, sorted(unmapped_ids)[:20])  # fmt: skip
    return out, skipped


def _catalog_lines(body: bytes) -> list[str]:
    """Best-effort readable listing of a tournaments/markets catalog response."""
    items = json.loads(body)
    if isinstance(items, dict):
        items = items.get("data", list(items.values()))
    lines = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        ident = next((it[k] for k in ("tournamentId", "marketId", "id") if k in it), "?")
        label = next(
            (it[k] for k in ("tournamentName", "marketName", "name", "slug") if k in it), ""
        )
        extra = {k: it[k] for k in ("tournamentSlug", "categoryName", "handicap", "outcomes",
                                    "upcomingFixtures") if k in it}  # fmt: skip
        lines.append(f"{ident}\t{label}\t{json.dumps(extra)[:200] if extra else ''}")
    return lines


class OddsPapiSource(Source):
    name = NAME

    def __init__(self, engine, config: SourceConfig, *, transport=None, sleep=None, clock=None):
        super().__init__(engine, config, clock)
        self.quota = (
            QuotaManager(engine, NAME, config.quota, **({"clock": clock} if clock else {}))
            if config.quota
            else None
        )
        extra = {k: v for k, v in dict(sleep=sleep, clock=clock).items() if v is not None}
        self.client = SourceClient(
            engine,
            NAME,
            config.options.get("base_url", "https://api.oddspapi.io/v4"),
            quota=self.quota,
            cost_parser=lambda _r: CostReport(cost=1),
            transport=transport,
            **extra,
        )

    @classmethod
    def jobs(cls) -> dict[str, JobSpec]:
        return {
            "odds": JobSpec("odds", "One bookmaker's board for a league (1 request)."),
            "tournaments": JobSpec("tournaments", "List tournament ids. --param sport_id=11"),
            "markets": JobSpec("markets", "List market ids. --param sport_id=11"),
        }

    def estimate_cost(self, job: str, params: dict[str, Any]) -> int:
        return 1

    def _request(self, job: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        opts = self.config.options
        if job in ("tournaments", "markets"):
            sport_id = params.get("sport_id") or opts.get("sport_ids", {}).get(params.get("sport"))
            if not sport_id:
                raise ConfigError(
                    f"{NAME}: give --param sport_id=<id> (14 football, 11 basketball)"
                )
            return f"/{job}", {"sportId": int(sport_id)}
        sport = params.get("sport")
        if not sport:
            raise ConfigError(f"{NAME}: --sport is required")
        tournament = (opts.get("tournaments") or {}).get(sport)
        if not tournament:
            raise NotConfigured(
                f"{NAME}: no tournament id for {sport}; find it with "
                f"`betdata run oddspapi tournaments --sport {sport}` and set "
                f"oddspapi.tournaments.{sport} in config/sources.yaml"
            )
        if not (opts.get("markets") or {}).get(sport):
            raise NotConfigured(f"{NAME}: no market map for {sport} in sources.yaml")
        return "/odds-by-tournaments", {
            "tournamentIds": str(tournament),
            "bookmaker": params.get("bookmaker") or opts.get("bookmaker", "pinnacle"),
        }

    def fetch(self, job: str, params: dict[str, Any], *, priority: str, run_id: int) -> RawInput:
        if not self.config.api_key:
            raise ConfigError(f"{self.config.api_key_env} is not set (see .env.example)")
        path, query = self._request(job, params)
        resp = self.client.get(
            path, {**query, "apiKey": self.config.api_key}, est_cost=1, priority=priority,
            run_id=run_id,
        )  # fmt: skip
        stored = {**query, **({"sport": params["sport"]} if params.get("sport") else {})}
        return RawInput(path, stored, resp.body, resp.fetched_at, resp.raw_id)

    def job_for_endpoint(self, endpoint: str) -> str:
        return {"/odds-by-tournaments": "odds"}.get(endpoint, endpoint.strip("/"))

    def _sport_for(self, params: dict[str, Any]) -> str:
        if params.get("sport"):
            return params["sport"]
        for sport, tid in (self.config.options.get("tournaments") or {}).items():
            if tid and str(tid) == str(params.get("tournamentIds")):
                return sport
        raise ParseError(
            f"{NAME}: can't tell the sport of tournament {params.get('tournamentIds')}"
        )

    def parse(self, job: str, raw: RawInput):
        if job in ("tournaments", "markets"):
            return _catalog_lines(raw.body)
        params = raw.params or {}
        sport = self._sport_for(params)
        opts = self.config.options
        home_first = opts.get("participant1_is_home", True)
        names = {"home": "participant1Name" if home_first else "participant2Name",
                 "away": "participant2Name" if home_first else "participant1Name"}  # fmt: skip
        batch, skipped = parse_board(
            raw.body,
            sport=sport,
            fetched_at=raw.fetched_at,
            bookmaker=params.get("bookmaker", opts.get("bookmaker", "pinnacle")),
            market_map={
                str(k): v for k, v in ((opts.get("markets") or {}).get(sport) or {}).items()
            },
            names=names,
        )
        if any(skipped.values()):
            log.info("%s %s skipped: %s", NAME, sport, {k: v for k, v in skipped.items() if v})
        return batch

    def load(self, conn, job: str, batch, *, run_id, raw_id) -> tuple[int, int]:
        if job in ("tournaments", "markets"):
            for line in batch:
                log.info("%s %s: %s", NAME, job, line)
            return 0, len(batch)
        stats = load_odds(conn, source=NAME, batch=batch, run_id=run_id, raw_id=raw_id)
        return stats.inserted + stats.removed, stats.prices_seen
