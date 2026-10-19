"""Open-Meteo weather for outdoor NFL and college football games — https://open-meteo.com

Free, no key, non-commercial use, data CC BY 4.0 (attribute Open-Meteo). ~10,000 calls/day;
requests with several locations or long date ranges count as several calls, so each request's
cost is estimated the same way (locations x 2-week blocks) before it is made.

  forecast  api.open-meteo.com/v1/forecast       games in the next 7 days; every run is kept,
                                                 so forecasts at different lead times survive
  observed  archive-api.open-meteo.com/v1/archive  reanalysis (ERA5), ~5 days behind, back to 1940
  hist_forecast  (M13, NFL) archived forecasts for finished games from 2022: the historical-
            forecast API (short lead) and the previous-runs API (the run 1-3 days before),
            one request each per venue and season; rows in nfl_weather_asof (nfl/weather.py)

Rows: kickoff hour plus the next 3 hours, in UTC (`timezone=GMT`; a response with any other
UTC offset is rejected). Units: degC, km/h, mm, %, degrees. Skipped: fixed domes (venue roof
type, or nflverse's per-game roof). Retractable roofs are included, since the roof state is
often decided on game day.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, insert, or_, select, update

from betdata.config import SourceConfig
from betdata.core.errors import ApiError, ConfigError, NothingToDo, ParseError
from betdata.core.http import CostReport, SourceClient
from betdata.core.quota import QuotaManager
from betdata.core.source import JobSpec, RawInput, Source
from betdata.db.schema import games, sports, venues, weather_observations

NAME = "open_meteo"
log = logging.getLogger(__name__)
VARIABLES = {
    "temperature_2m": "temp_c",
    "relative_humidity_2m": "humidity_pct",
    "precipitation": "precip_mm",
    "wind_speed_10m": "wind_kph",
    "wind_gusts_10m": "gust_kph",
    "wind_direction_10m": "wind_dir_deg",
}
GAME_HOURS = 4  # kickoff hour + 3
ARCHIVE_DELAY = timedelta(days=6)
FORECAST_LOCATIONS_PER_REQUEST = 25
ARCHIVE_LOCATIONS_PER_REQUEST = 10
SPORTS = ("nfl", "ncaaf")


@dataclass(frozen=True)
class Target:
    game_id: int
    kickoff: datetime
    lat: float
    lon: float


@dataclass
class RequestPlan:
    kind: str  # forecast | observed
    locations: list[tuple[float, float]]
    start_date: str
    end_date: str
    games: list[list] = field(default_factory=list)  # [location index, game_id, kickoff iso]

    @property
    def cost(self) -> int:
        days = (
            datetime.fromisoformat(self.end_date) - datetime.fromisoformat(self.start_date)
        ).days + 1
        return len(self.locations) * max(1, math.ceil(days / 14))


def _kickoff_hour(t: datetime) -> datetime:
    return t.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _date_span(t: datetime) -> tuple[str, str]:
    start = _kickoff_hour(t)
    end = start + timedelta(hours=GAME_HOURS - 1)
    return start.date().isoformat(), end.date().isoformat()


def plan_requests(targets: list[Target], kind: str) -> list[RequestPlan]:
    """Forecast: all locations share the window, so batch by location. Archive: batch games
    that share the same dates, so each request covers only the days needed."""
    plans: list[RequestPlan] = []
    if kind == "forecast":
        groups = {("all",): targets}
        per_request = FORECAST_LOCATIONS_PER_REQUEST
    else:
        groups: dict[tuple, list[Target]] = {}
        for t in targets:
            groups.setdefault(_date_span(t.kickoff), []).append(t)
        per_request = ARCHIVE_LOCATIONS_PER_REQUEST
    for group in groups.values():
        by_location: dict[tuple[float, float], list[Target]] = {}
        for t in group:
            by_location.setdefault((round(t.lat, 4), round(t.lon, 4)), []).append(t)
        locations = sorted(by_location)
        for i in range(0, len(locations), per_request):
            chunk = locations[i : i + per_request]
            chunk_targets = [t for loc in chunk for t in by_location[loc]]
            spans = [_date_span(t.kickoff) for t in chunk_targets]
            plan = RequestPlan(kind, chunk, min(s[0] for s in spans), max(s[1] for s in spans))
            plan.games = [
                [chunk.index((round(t.lat, 4), round(t.lon, 4))), t.game_id, t.kickoff.isoformat()]
                for t in chunk_targets
            ]
            plans.append(plan)
    return plans


def hist_plans(targets: list[Target]) -> list[RequestPlan]:
    """One request per venue and season, from its first to its last game's hours."""
    groups: dict[tuple, list[Target]] = {}
    for t in targets:
        season = t.kickoff.year if t.kickoff.month >= 3 else t.kickoff.year - 1
        groups.setdefault((round(t.lat, 4), round(t.lon, 4), season), []).append(t)
    plans = []
    for (lat, lon, _), ts in sorted(groups.items()):
        spans = [_date_span(t.kickoff) for t in ts]
        plan = RequestPlan("hist_forecast", [(lat, lon)], min(s[0] for s in spans),
                           max(s[1] for s in spans))  # fmt: skip
        plan.games = [[t.game_id, t.kickoff.isoformat()] for t in ts]
        plans.append(plan)
    return plans


def parse_hist(data: dict, raw_id: int | None) -> WeatherRows:
    from betdata.nfl.weather import forecast_rows

    out = WeatherRows()
    try:
        for page in data["pages"]:
            fetched = datetime.fromisoformat(page["fetched_at"])
            for game_id, kickoff_iso in page["meta"]["games"]:
                out.rows += forecast_rows(game_id, datetime.fromisoformat(kickoff_iso),
                                          page.get("hf"), page.get("prev"), fetched, raw_id)
    except (KeyError, TypeError, ValueError) as e:
        raise ParseError(f"{NAME}: unexpected archived-forecast response: {e!r}") from e
    return out


@dataclass
class WeatherRows:
    rows: list[dict[str, Any]] = field(default_factory=list)
    elevations: dict[int, float] = field(default_factory=dict)  # game_id -> grid elevation


def parse_page(body: Any, meta: dict, fetched_at: datetime) -> WeatherRows:
    out = WeatherRows()
    responses = body if isinstance(body, list) else [body]
    kind = meta["kind"]
    for loc_index, game_id, kickoff_iso in meta["games"]:
        try:
            resp = responses[loc_index]
            if resp.get("utc_offset_seconds", 0) != 0:
                raise ParseError(
                    f"{NAME}: expected GMT times, got offset {resp['utc_offset_seconds']}"
                )
            hourly = resp["hourly"]
            index = {t: i for i, t in enumerate(hourly["time"])}
        except (IndexError, KeyError, TypeError) as e:
            raise ParseError(f"{NAME}: unexpected response shape: {e!r}") from e
        kickoff = datetime.fromisoformat(kickoff_iso)
        start = _kickoff_hour(kickoff)
        if resp.get("elevation") is not None:
            out.elevations[game_id] = float(resp["elevation"])
        for h in range(GAME_HOURS):
            valid = start + timedelta(hours=h)
            i = index.get(valid.strftime("%Y-%m-%dT%H:%M"))
            if i is None:
                continue
            row = {col: hourly.get(var, [None] * (i + 1))[i] for var, col in VARIABLES.items()}
            if all(v is None for v in row.values()):
                continue
            out.rows.append(
                dict(
                    row,
                    game_id=game_id,
                    source=NAME,
                    kind=kind,
                    fetched_at=fetched_at,
                    valid_time=valid,
                    lead_hours=round((kickoff - fetched_at).total_seconds() / 3600, 2)
                    if kind == "forecast"
                    else None,
                )
            )
    return out


class OpenMeteoSource(Source):
    name = NAME

    def __init__(self, engine, config: SourceConfig, *, transport=None, sleep=None, clock=None):
        super().__init__(engine, config, clock)
        self.quota = (
            QuotaManager(engine, NAME, config.quota, **({"clock": clock} if clock else {}))
            if config.quota
            else None
        )
        extra = {k: v for k, v in dict(sleep=sleep, clock=clock).items() if v is not None}
        common = dict(
            quota=self.quota,
            cost_parser=lambda _r: CostReport(),
            transport=transport,
            min_interval=float(config.options.get("min_interval_seconds", 1)),
            **extra,
        )
        opts = config.options
        self.forecast = SourceClient(
            engine, NAME, opts.get("forecast_url", "https://api.open-meteo.com/v1"),
            label="forecast:", **common,
        )  # fmt: skip
        self.archive = SourceClient(
            engine, NAME, opts.get("archive_url", "https://archive-api.open-meteo.com/v1"),
            label="archive:", **common,
        )  # fmt: skip
        self.hist = SourceClient(
            engine, NAME, opts.get("historical_forecast_url",
                                   "https://historical-forecast-api.open-meteo.com/v1"),
            label="hist:", **common,
        )  # fmt: skip
        self.prev = SourceClient(
            engine, NAME, opts.get("previous_runs_url", "https://previous-runs-api.open-meteo.com/v1"),
            label="prev:", **common,
        )  # fmt: skip

    def close(self) -> None:
        for c in (self.forecast, self.archive, self.hist, self.prev):
            c.close()

    @classmethod
    def jobs(cls) -> dict[str, JobSpec]:
        return {
            "forecast": JobSpec("forecast", "Forecasts for outdoor games in the next 7 days."),
            "observed": JobSpec(
                "observed",
                "Reanalysis weather for finished outdoor games that lack it. "
                "--seasons 2020-2025 for history; default: the last 45 days.",
            ),
            "hist_forecast": JobSpec(
                "hist_forecast",
                "Archived forecasts as of each prediction point for finished outdoor NFL games "
                "(2022+), into nfl_weather_asof. --param seasons=2022-2025.",
            ),
        }

    def estimate_cost(self, job: str, params: dict[str, Any]) -> int:
        """Weighted cost of one run, from the games needing weather right now (for projections;
        each request is still checked against the quota with its own exact cost)."""
        if job == "hist_forecast":
            return sum(p.cost * 3 for p in hist_plans(self.hist_targets(params)))
        try:
            targets = self.targets(job, params)
        except ConfigError:
            return 1
        return max(
            1,
            sum(
                p.cost
                for p in plan_requests(targets, "forecast" if job == "forecast" else "observed")
            ),
        )

    # -- target selection ----------------------------------------------------------

    def targets(self, job: str, params: dict[str, Any]) -> list[Target]:
        sport = params.get("sport")
        if sport not in SPORTS:
            raise ConfigError(f"{NAME}: --sport must be one of {SPORTS}")
        now = self.clock()
        q = (
            select(games.c.game_id, games.c.start_time_utc, venues.c.lat, venues.c.lon)
            .join(venues, venues.c.venue_id == games.c.venue_id)
            .join(sports, sports.c.sport_id == games.c.sport_id)
            .where(
                sports.c.key == sport,
                venues.c.lat.is_not(None),
                venues.c.lon.is_not(None),
                or_(venues.c.roof_type.is_(None), venues.c.roof_type != "dome"),
                or_(games.c.roof.is_(None), games.c.roof != "dome"),
            )
        )
        if job == "forecast":
            days = int(params.get("days", 7))
            q = q.where(
                games.c.status == "scheduled",
                games.c.start_time_utc > now,
                games.c.start_time_utc <= now + timedelta(days=days),
            )
        else:
            already = exists().where(
                and_(
                    weather_observations.c.game_id == games.c.game_id,
                    weather_observations.c.source == NAME,
                    weather_observations.c.kind == "observed",
                )  # fmt: skip
            )
            q = q.where(
                ~already,
                games.c.status.not_in(("postponed", "cancelled")),
                games.c.start_time_utc <= now - ARCHIVE_DELAY,
            )
            if params.get("seasons"):
                from betdata.sources.nflverse import parse_seasons

                q = q.where(games.c.season.in_(sorted(parse_seasons(params["seasons"]) or [])))
            else:
                q = q.where(games.c.start_time_utc > now - timedelta(days=45))
        with self.engine.connect() as conn:
            return [Target(*r) for r in conn.execute(q.order_by(games.c.start_time_utc))]

    # -- source interface ----------------------------------------------------------

    # -- M13: archived forecasts as of each prediction point (NFL) -------------------

    def hist_targets(self, params: dict[str, Any]) -> list[Target]:
        from betdata.db.schema import nfl_weather_asof
        from betdata.nfl.weather import FORECAST_FIRST_SEASON
        from betdata.sources.nflverse import parse_seasons

        spec = params.get("seasons") or f"{FORECAST_FIRST_SEASON}-{self.clock().year}"
        seasons = sorted(parse_seasons(spec) or [])
        have = exists().where(and_(
            nfl_weather_asof.c.game_id == games.c.game_id,
            nfl_weather_asof.c.source.in_(("open_meteo_prev", "open_meteo_hf"))))  # fmt: skip
        q = (
            select(games.c.game_id, games.c.start_time_utc, venues.c.lat, venues.c.lon)
            .join(venues, venues.c.venue_id == games.c.venue_id)
            .join(sports, sports.c.sport_id == games.c.sport_id)
            .where(sports.c.key == "nfl", games.c.status == "final", games.c.season.in_(seasons),
                   venues.c.lat.is_not(None), venues.c.lon.is_not(None),
                   or_(venues.c.roof_type.is_(None), venues.c.roof_type != "dome"),
                   or_(games.c.roof.is_(None), games.c.roof != "dome"), ~have)
            .order_by(games.c.start_time_utc)
        )  # fmt: skip
        with self.engine.connect() as conn:
            return [Target(*r) for r in conn.execute(q)]

    def _fetch_hist(self, params, priority, run_id) -> RawInput:
        from betdata.nfl.weather import HOURLY, MAX_LEAD_DAYS

        plans = hist_plans(self.hist_targets(params))
        if not plans:
            raise NothingToDo(f"{NAME}: no finished outdoor NFL games need archived forecasts")
        plans = plans[: int(params.get("max_requests", 200))]
        prev_vars = [f"{v}_previous_day{n}" for v in HOURLY for n in range(1, MAX_LEAD_DAYS + 1)]
        pages, last = [], None
        for plan in plans:
            lat, lon = plan.locations[0]
            base = {"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "timezone": "GMT",
                    "wind_speed_unit": "kmh", "start_date": plan.start_date,
                    "end_date": plan.end_date}  # fmt: skip
            meta = {"kind": "hist_forecast", "games": plan.games}
            page = {"meta": meta}
            for key, client, variables, mult in (("hf", self.hist, HOURLY, 1),
                                                 ("prev", self.prev, prev_vars, 2)):  # fmt: skip
                try:
                    resp = client.get("/forecast", {**base, "hourly": ",".join(variables)},
                                      est_cost=plan.cost * mult, priority=priority,
                                      run_id=run_id, archive_meta=meta)  # fmt: skip
                except ApiError as e:  # e.g. dates the archive does not cover
                    log.warning("%s hist_forecast %s %s..%s: %s", NAME, key, plan.start_date,
                                plan.end_date, e)  # fmt: skip
                    page[key] = None
                    continue
                page[key] = json.loads(resp.body)
                page["fetched_at"] = resp.fetched_at.isoformat()
                last = resp
            if "fetched_at" in page:
                pages.append(page)
        if last is None:
            raise ApiError(f"{NAME}: no archived forecast request succeeded")
        body = json.dumps({"pages": pages}).encode()
        return RawInput("hist:/forecast", {"sport": "nfl"}, body, last.fetched_at, last.raw_id)

    def fetch(self, job: str, params: dict[str, Any], *, priority: str, run_id: int) -> RawInput:
        if job == "hist_forecast":
            return self._fetch_hist(params, priority, run_id)
        kind = "forecast" if job == "forecast" else "observed"
        targets = self.targets(job, params)
        if not targets:
            raise NothingToDo(f"{NAME}: no outdoor {params.get('sport')} games need {kind} weather")
        plans = plan_requests(targets, kind)
        limit = int(params.get("max_requests", 200))
        if len(plans) > limit:
            log.info("%s: %d requests needed; doing %d this run", NAME, len(plans), limit)
            plans = plans[:limit]
        client, path = (
            (self.forecast, "/forecast") if kind == "forecast" else (self.archive, "/archive")
        )
        pages, last = [], None
        for plan in plans:
            query = {
                "latitude": ",".join(f"{lat:.4f}" for lat, _ in plan.locations),
                "longitude": ",".join(f"{lon:.4f}" for _, lon in plan.locations),
                "hourly": ",".join(VARIABLES),
                "timezone": "GMT",
                "wind_speed_unit": "kmh",
                "start_date": plan.start_date,
                "end_date": plan.end_date,
            }
            meta = {"kind": kind, "games": plan.games}
            resp = client.get(path, query, est_cost=plan.cost, priority=priority, run_id=run_id,
                              archive_meta=meta)  # fmt: skip
            pages.append({"meta": meta, "fetched_at": resp.fetched_at.isoformat(),
                          "body": json.loads(resp.body)})  # fmt: skip
            last = resp
        body = json.dumps({"pages": pages}).encode()
        return RawInput(client.label + path, {"sport": params.get("sport")}, body,
                        last.fetched_at, last.raw_id)  # fmt: skip

    def job_for_endpoint(self, endpoint: str) -> str:
        if endpoint.startswith(("hist:", "prev:")):
            return "hist_forecast"
        return "forecast" if endpoint.startswith("forecast:") else "observed"

    def parse(self, job: str, raw: RawInput) -> WeatherRows:
        if job == "hist_forecast":
            return parse_hist(json.loads(raw.body), raw.raw_id)
        data = json.loads(raw.body)
        if isinstance(data, dict) and "pages" in data:
            pages = [
                (p["body"], p["meta"], datetime.fromisoformat(p["fetched_at"]))
                for p in data["pages"]
            ]
        else:  # one archived response, replayed: its games are in the stored params
            meta = (raw.params or {}).get("_meta")
            if not meta:
                raise ParseError(f"{NAME}: stored response has no game metadata")
            pages = [(data, meta, raw.fetched_at)]
        out = WeatherRows()
        for body, meta, fetched_at in pages:
            page = parse_page(body, meta, fetched_at)
            out.rows += page.rows
            out.elevations.update(page.elevations)
        return out

    def load(self, conn, job: str, batch: WeatherRows, *, run_id, raw_id) -> tuple[int, int]:
        if job == "hist_forecast":
            from betdata.nfl.weather import insert_new

            return insert_new(conn, batch.rows), len({r["game_id"] for r in batch.rows})
        """Insert one fetch's rows per game unless that game/kind/fetch time is already stored
        (so replaying a response is a no-op), then fill missing venue elevations."""
        stored = {
            (r.game_id, r.kind, r.fetched_at)
            for r in conn.execute(
                select(
                    weather_observations.c.game_id,
                    weather_observations.c.kind,
                    weather_observations.c.fetched_at,
                ).where(
                    weather_observations.c.source == NAME,
                    weather_observations.c.game_id.in_({r["game_id"] for r in batch.rows} or {-1}),
                )
            )
        }
        new = [r for r in batch.rows if (r["game_id"], r["kind"], r["fetched_at"]) not in stored]
        if new:
            conn.execute(insert(weather_observations), [dict(r, raw_id=raw_id) for r in new])
        for game_id, elevation in batch.elevations.items():
            venue_id = conn.execute(
                select(games.c.venue_id).where(games.c.game_id == game_id)
            ).scalar()
            conn.execute(
                update(venues)
                .where(venues.c.venue_id == venue_id, venues.c.elevation_m.is_(None))
                .values(elevation_m=elevation)
            )
        return len(new), len({r["game_id"] for r in batch.rows})
