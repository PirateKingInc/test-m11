"""NBA's own data feeds on cdn.nba.com — the backend nba_api reads for live data.

Fetched through our client (retries, logging, raw archive) instead of the nba_api package.
Structures follow nba_api 1.11: the season schedule is nba_api's `ScheduleLeagueV2` shape and
the scoreboard its `live.nba.endpoints.ScoreBoard` shape.

  schedule    /staticData/scheduleLeagueV2.json        whole season: times, status, scores
  scoreboard  /liveData/scoreboard/todaysScoreboard_00.json   today's games, same-night finals

nba_cdn is the NBA schedule authority. If NBA's servers refuse this machine (stats.nba.com is
known to block cloud IPs; the CDN usually doesn't), the sportsdataverse source keeps NBA
schedules and scores flowing from ESPN data with up to a day's delay.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from betdata.config import SourceConfig
from betdata.core.errors import ParseError
from betdata.core.http import SourceClient
from betdata.core.matching import season_for
from betdata.core.models import ParsedEvent, ParsedOdds
from betdata.core.odds_loader import load_odds
from betdata.core.results_loader import load_results
from betdata.core.source import JobSpec, RawInput, Source

NAME = "nba_cdn"
log = logging.getLogger(__name__)
ENDPOINTS = {
    "schedule": "/staticData/scheduleLeagueV2.json",
    "scoreboard": "/liveData/scoreboard/todaysScoreboard_00.json",
}
# gameId prefix -> season type; 003 (All-Star) is skipped
GAME_TYPES = {"001": "PRE", "002": "REG", "004": "POST", "005": "POST", "006": "REG"}


def _status(game: dict) -> str:
    text = str(game.get("gameStatusText") or "").lower()
    if "ppd" in text or "postponed" in text:
        return "postponed"
    if "cancel" in text:
        return "cancelled"
    return {1: "scheduled", 2: "in_progress", 3: "final"}.get(game.get("gameStatus"), "scheduled")


def _event(game: dict, start_key: str) -> ParsedEvent | None:
    gid = str(game["gameId"])
    season_type = GAME_TYPES.get(gid[:3])
    if season_type is None:
        return None
    home, away = game["homeTeam"], game["awayTeam"]
    start = datetime.fromisoformat(str(game[start_key]).replace("Z", "+00:00"))
    status = _status(game)
    final = status == "final" and home.get("score") is not None and away.get("score") is not None
    return ParsedEvent(
        source_game_id=gid,
        start_time_utc=start,
        home_name=home["teamTricode"],
        away_name=away["teamTricode"],
        neutral_site=bool(game.get("isNeutral")) if "isNeutral" in game else None,
        season=season_for("nba", start),
        season_type=season_type,
        home_score=int(home["score"]) if final else None,
        away_score=int(away["score"]) if final else None,
        final=final,
        status=status,
    )


def parse_schedule(body: bytes, fetched_at: datetime) -> ParsedOdds:
    try:
        data = json.loads(body)
        league = data.get("leagueSchedule") or data[list(data)[1]]
        games = [g for day in league["gameDates"] for g in day["games"]]
        events = [e for e in (_event(g, "gameDateTimeUTC") for g in games) if e]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise ParseError(f"{NAME}: unexpected schedule shape: {e!r}") from e
    return ParsedOdds(sport="nba", fetched_at=fetched_at, events=events, schedule_authority=True)


def parse_scoreboard(body: bytes, fetched_at: datetime) -> ParsedOdds:
    try:
        games = json.loads(body)["scoreboard"]["games"]
        events = [e for e in (_event(g, "gameTimeUTC") for g in games) if e]
    except (ValueError, KeyError, TypeError) as e:
        raise ParseError(f"{NAME}: unexpected scoreboard shape: {e!r}") from e
    return ParsedOdds(sport="nba", fetched_at=fetched_at, events=events, schedule_authority=True)


class NbaCdnSource(Source):
    name = NAME

    def __init__(self, engine, config: SourceConfig, *, transport=None, sleep=None, clock=None):
        super().__init__(engine, config, clock)
        extra = {k: v for k, v in dict(sleep=sleep, clock=clock).items() if v is not None}
        self.client = SourceClient(
            engine,
            NAME,
            config.options.get("base_url", "https://cdn.nba.com/static/json"),
            transport=transport,
            headers=config.options.get("headers") or {},
            **extra,
        )

    @classmethod
    def jobs(cls) -> dict[str, JobSpec]:
        return {
            "schedule": JobSpec("schedule", "Whole NBA season: times, status, scores."),
            "scoreboard": JobSpec("scoreboard", "Today's games, for same-night final scores."),
        }

    def fetch(self, job: str, params: dict[str, Any], *, priority: str, run_id: int) -> RawInput:
        path = ENDPOINTS[job]
        resp = self.client.get(path, est_cost=0, run_id=run_id, dedupe=True)
        return RawInput(path, None, resp.body, resp.fetched_at, resp.raw_id)

    def job_for_endpoint(self, endpoint: str) -> str:
        return {v: k for k, v in ENDPOINTS.items()}[endpoint]

    def parse(self, job: str, raw: RawInput) -> ParsedOdds:
        parser = parse_schedule if job == "schedule" else parse_scoreboard
        return parser(raw.body, raw.fetched_at)

    def load(self, conn, job: str, batch: ParsedOdds, *, run_id, raw_id) -> tuple[int, int]:
        stats = load_odds(conn, source=NAME, batch=batch, run_id=run_id, raw_id=raw_id)
        changed = load_results(conn, source=NAME, events=batch.events, matches=stats.matches)
        return changed, len(batch.events)
