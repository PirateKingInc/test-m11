"""nflverse — https://github.com/nflverse/nflverse-data

Reads the same release files as nflreadpy (`nflreadpy.load_schedules()` downloads
`schedules/games.csv`), but through our client so downloads get retries, logging and the raw
archive. No quota; files are refreshed by nflverse during the season.

Schedules (`games.csv`, every season since 1999) give us:
  * the authoritative NFL schedule: season, week, game type, kickoff, neutral site
  * final scores
  * lines (book unspecified by nflverse). For games not yet played these are current lines and
    are stored as `poll` snapshots; once a game is final they are its closing line and are
    stored as `source_closing`.

Conventions in the file: `spread_line` is the expected home margin (positive = home favored),
so the home team's spread is -spread_line. `gameday`/`gametime` are US Eastern.
"""

from __future__ import annotations

import csv
import io
import os
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from betdata.config import PROJECT_ROOT, SourceConfig
from betdata.core.errors import ConfigError, ParseError
from betdata.core.http import SourceClient
from betdata.core.matching import season_for
from betdata.core.models import ParsedEvent, ParsedOdds, ParsedPrice
from betdata.core.normalize.odds_math import american_to_decimal
from betdata.core.odds_loader import load_odds
from betdata.core.results_loader import load_results
from betdata.core.source import JobSpec, RawInput, Source
from betdata.db.schema import utcnow
from betdata.sources import nflverse_context, nflverse_data

NAME = "nflverse"
BOOK_KEY = "nflverse"
BOOK_TITLE = "nflverse line (book unspecified)"
EASTERN = ZoneInfo("America/New_York")
DEFAULT_KICKOFF = time(13, 0)  # used for old games with no recorded kickoff time
SCHEDULES_ENDPOINT = "/schedules/games.csv"
REQUIRED_COLUMNS = {
    "game_id", "season", "game_type", "week", "gameday", "gametime", "away_team", "home_team",
    "away_score", "home_score", "location", "away_moneyline", "home_moneyline", "spread_line",
    "away_spread_odds", "home_spread_odds", "total_line", "under_odds", "over_odds",
}  # fmt: skip
SEASON_TYPES = {"REG": "REG", "WC": "POST", "DIV": "POST", "CON": "POST", "SB": "POST"}


def parse_seasons(spec: str | None, *, now: datetime | None = None) -> set[int] | None:
    """'2026' -> {2026}; '2020-2026' -> range; 'all' -> None (no filter); None -> current."""
    if spec is None:
        return {season_for("nfl", now or utcnow())}
    spec = spec.strip().lower()
    if spec == "all":
        return None
    out: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def _value(row: dict[str, str], key: str) -> str | None:
    v = (row.get(key) or "").strip()
    return None if v in ("", "NA") else v


def _int(row, key) -> int | None:
    v = _value(row, key)
    return None if v is None else int(float(v))


def _float(row, key) -> float | None:
    v = _value(row, key)
    return None if v is None else float(v)


def _kickoff_utc(row) -> datetime:
    day = datetime.strptime(row["gameday"], "%Y-%m-%d").date()
    t = _value(row, "gametime")
    clock = datetime.strptime(t, "%H:%M").time() if t else DEFAULT_KICKOFF
    return datetime.combine(day, clock, tzinfo=EASTERN).astimezone(ZoneInfo("UTC"))


def _price(gid, market, outcome, line, american, kind) -> ParsedPrice:
    return ParsedPrice(
        source_game_id=gid,
        book_key=BOOK_KEY,
        book_title=BOOK_TITLE,
        market_key=market,
        outcome=outcome,
        line=line,
        price_american=american,
        price_decimal=american_to_decimal(american) if american is not None else None,
        snapshot_kind=kind,
    )


def parse_schedules(body: bytes, *, fetched_at: datetime, seasons: set[int] | None) -> ParsedOdds:
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise ParseError(f"{NAME}: games.csv is missing columns {sorted(missing)}")

    out = ParsedOdds(sport="nfl", fetched_at=fetched_at, schedule_authority=True)
    try:
        for row in reader:
            season = int(row["season"])
            if seasons is not None and season not in seasons:
                continue
            gid = row["game_id"]
            home_score, away_score = _int(row, "home_score"), _int(row, "away_score")
            final = home_score is not None and away_score is not None
            out.events.append(
                ParsedEvent(
                    source_game_id=gid,
                    start_time_utc=_kickoff_utc(row),
                    home_name=row["home_team"],
                    away_name=row["away_team"],
                    neutral_site=row["location"] == "Neutral",
                    season=season,
                    season_type=SEASON_TYPES.get(row["game_type"], row["game_type"]),
                    week=_int(row, "week"),
                    home_score=home_score,
                    away_score=away_score,
                    final=final,
                    venue_source_id=_value(row, "stadium_id"),
                    venue_name=_value(row, "stadium"),
                    roof=_value(row, "roof"),
                )
            )
            kind = "source_closing" if final else "poll"
            home_ml, away_ml = _int(row, "home_moneyline"), _int(row, "away_moneyline")
            if home_ml is not None and away_ml is not None:
                out.prices += [
                    _price(gid, "h2h", "home", None, home_ml, kind),
                    _price(gid, "h2h", "away", None, away_ml, kind),
                ]
            spread = _float(row, "spread_line")
            if spread is not None:
                out.prices += [
                    _price(
                        gid, "spreads", "home", -spread or 0.0, _int(row, "home_spread_odds"), kind
                    ),
                    _price(gid, "spreads", "away", spread, _int(row, "away_spread_odds"), kind),
                ]
            total = _float(row, "total_line")
            if total is not None:
                out.prices += [
                    _price(gid, "totals", "over", total, _int(row, "over_odds"), kind),
                    _price(gid, "totals", "under", total, _int(row, "under_odds"), kind),
                ]
    except (KeyError, ValueError) as e:
        raise ParseError(f"{NAME}: bad row in games.csv: {e}") from e
    return out


def _season_from_endpoint(endpoint: str) -> int:
    """'/snap_counts/snap_counts_2024.csv' -> 2024 (replays of stored files)."""
    return int(endpoint.rsplit("_", 1)[1].split(".")[0])


class NflverseSource(Source):
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
        self.client = SourceClient(
            engine,
            NAME,
            config.options.get(
                "base_url", "https://github.com/nflverse/nflverse-data/releases/download"
            ),
            transport=transport,
            timeout=120.0,
            **extra,
        )

    @classmethod
    def jobs(cls) -> dict[str, JobSpec]:
        return {
            "schedules": JobSpec(
                "schedules",
                "Schedule, scores and lines. --seasons 2026 | 2015-2026 | all (default: current).",
            ),
            "injuries": JobSpec("injuries", "Injury reports for one season (default: current)."),
            "pbp": JobSpec("pbp", "Play-by-play parquet for one season, saved under data/pbp."),
            "team_stats": JobSpec("team_stats", "Team pass/rush volume and EPA per game (1999+)."),
            "snaps": JobSpec("snaps", "Snaps per player per game (2012+)."),
            "rosters": JobSpec("rosters", "Weekly roster status per player (2002+)."),
        }

    def _one_season(self, params) -> int:
        seasons = parse_seasons(params.get("seasons"), now=self.clock())
        if not seasons or len(seasons) != 1:
            raise ConfigError(
                f"{NAME}: one season per run; for several use "
                "`betdata backfill --source nflverse --dataset ... --seasons ...`"
            )
        return seasons.pop()

    def fetch(self, job: str, params: dict[str, Any], *, priority: str, run_id: int) -> RawInput:
        if job == "injuries":
            season = self._one_season(params)
            path = f"/injuries/injuries_{season}.csv"
            resp = self.client.get(path, est_cost=0, run_id=run_id, dedupe=True)
            return RawInput(path, {"season": season}, resp.body, resp.fetched_at, resp.raw_id)
        if job == "pbp":
            season = self._one_season(params)
            path = f"/pbp/play_by_play_{season}.parquet"
            resp = self.client.get(path, est_cost=0, run_id=run_id, store_raw=False)
            return RawInput(path, {"season": season}, resp.body, resp.fetched_at, None)
        if job in nflverse_context.DATASETS:
            season = self._one_season(params)
            path = nflverse_context.DATASETS[job][0].format(season=season)
            resp = self.client.get(path, est_cost=0, run_id=run_id, dedupe=True)
            return RawInput(path, {"season": season}, resp.body, resp.fetched_at, resp.raw_id)
        resp = self.client.get(SCHEDULES_ENDPOINT, est_cost=0, run_id=run_id, dedupe=True)
        # The season filter is applied when parsing; the stored raw file is always complete.
        return RawInput(
            SCHEDULES_ENDPOINT,
            {"seasons": params.get("seasons")},
            resp.body,
            resp.fetched_at,
            resp.raw_id,
        )

    def job_for_endpoint(self, endpoint: str) -> str:
        if endpoint.startswith("/injuries/"):
            return "injuries"
        for job, (template, _) in nflverse_context.DATASETS.items():
            if endpoint.startswith(template.split("{")[0]):
                return job
        return "schedules"

    def parse(self, job: str, raw: RawInput):
        if job == "injuries":
            return nflverse_data.parse_injuries(raw.body, fetched_at=raw.fetched_at)
        if job in nflverse_context.DATASETS:
            season = (raw.params or {}).get("season") or _season_from_endpoint(raw.endpoint)
            return nflverse_context.parse(job, raw.body, season=season, fetched_at=raw.fetched_at)
        if job == "pbp":
            url = str(self.client._http.base_url).rstrip("/") + raw.endpoint
            return nflverse_data.PbpFile(raw.params["season"], url, raw.body, raw.fetched_at)
        spec = (raw.params or {}).get("seasons")
        # Replays of stored files (no filter recorded) load every season.
        seasons = parse_seasons(spec, now=self.clock()) if raw.params else None
        return parse_schedules(raw.body, fetched_at=raw.fetched_at, seasons=seasons)

    def load(self, conn, job: str, batch, *, run_id, raw_id) -> tuple[int, int]:
        if job == "injuries":
            return nflverse_data.load_injuries(conn, batch, raw_id=raw_id)
        if job in nflverse_context.DATASETS:
            return nflverse_context.load(conn, batch, raw_id=raw_id)
        if job == "pbp":
            data_dir = Path(
                self.config.options.get("data_dir")
                or os.environ.get("BETDATA_DATA_DIR")
                or PROJECT_ROOT / "data"
            )
            return nflverse_data.store_pbp(conn, batch, data_dir)
        stats = load_odds(conn, source=NAME, batch=batch, run_id=run_id, raw_id=raw_id)
        changed = load_results(conn, source=NAME, events=batch.events, matches=stats.matches)
        return stats.inserted + stats.removed + changed, stats.events
