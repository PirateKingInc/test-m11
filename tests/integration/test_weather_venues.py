"""M8: venues (NFL seed, CFBD) and Open-Meteo forecasts / observed weather."""

import csv
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from betdata.config import PROJECT_ROOT
from betdata.db.schema import games, raw_responses, venues, weather_observations
from betdata.sources.cfbd import CfbdSource
from betdata.sources.nflverse import NflverseSource
from betdata.sources.open_meteo import OpenMeteoSource, RequestPlan, Target, plan_requests
from tests.conftest import fixture_bytes

# Rough country boxes (lat_min, lat_max, lon_min, lon_max): catch typos and flipped signs.
BOXES = {
    "US": (18, 50, -160, -66), "CA": (41, 70, -141, -52), "GB": (49, 61, -9, 2),
    "DE": (47, 55, 5, 16), "MX": (14, 33, -118, -86), "BR": (-34, 6, -74, -34),
    "ES": (35, 44, -10, 5), "FR": (41, 52, -5, 10), "AU": (-44, -10, 112, 154),
}  # fmt: skip


class FakeOpenMeteo:
    """Answers like Open-Meteo: one object for one location, a list for several."""

    def __init__(self, offset=0):
        self.requests: list[httpx.Request] = []
        self.offset = offset

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        q = request.url.params
        lats = [float(x) for x in q["latitude"].split(",")]
        lons = [float(x) for x in q["longitude"].split(",")]
        start = datetime.fromisoformat(q["start_date"]).replace(tzinfo=UTC)
        end = datetime.fromisoformat(q["end_date"]).replace(tzinfo=UTC) + timedelta(hours=23)
        hours = []
        t = start
        while t <= end:
            hours.append(t)
            t += timedelta(hours=1)
        out = []
        for i, (lat, lon) in enumerate(zip(lats, lons, strict=True)):
            out.append({
                "latitude": lat, "longitude": lon, "generationtime_ms": 0.2,
                "utc_offset_seconds": self.offset, "timezone": "GMT",
                "elevation": 1609.0 if lat > 39.7 and lon < -104 else 100.0,
                "hourly_units": {"time": "iso8601", "temperature_2m": "°C"},
                "hourly": {
                    "time": [h.strftime("%Y-%m-%dT%H:%M") for h in hours],
                    "temperature_2m": [10.0 + i + h.hour / 100 for h in hours],
                    "relative_humidity_2m": [55] * len(hours),
                    "precipitation": [0.0] * len(hours),
                    "wind_speed_10m": [float(h.hour) for h in hours],
                    "wind_gusts_10m": [float(h.hour) + 10 for h in hours],
                    "wind_direction_10m": [270] * len(hours),
                },
            })  # fmt: skip
        return httpx.Response(200, json=out[0] if len(out) == 1 else out)


@pytest.fixture
def meteo():
    return FakeOpenMeteo()


@pytest.fixture
def weather(engine, settings, meteo, clock):
    return OpenMeteoSource(
        engine,
        settings.source("open_meteo"),
        transport=httpx.MockTransport(meteo),
        sleep=lambda s: None,
        clock=clock,
    )


@pytest.fixture
def nfl_week(engine, settings, fake_api, clock):
    src = NflverseSource(engine, settings.source("nflverse"), transport=fake_api.transport,
                         clock=clock)  # fmt: skip
    fake_api.add(body=fixture_bytes("nflverse", "games_sample.csv"))
    assert src.run("schedules", seasons="all").status == "ok"


def rows(engine, *where):
    with engine.connect() as conn:
        return conn.execute(
            select(weather_observations).where(*where).order_by(weather_observations.c.weather_id)
        ).mappings().all()  # fmt: skip


def venue_of(engine, nflverse_game):
    with engine.connect() as conn:
        from betdata.db.schema import game_source_ids

        return conn.execute(
            select(venues, games.c.roof)
            .join(games, games.c.venue_id == venues.c.venue_id)
            .join(game_source_ids, game_source_ids.c.game_id == games.c.game_id)
            .where(game_source_ids.c.source_game_id == nflverse_game)
        ).mappings().one()  # fmt: skip


# --- venues ------------------------------------------------------------------------


def test_nfl_venue_seed_is_sane():
    rows_ = list(csv.DictReader((PROJECT_ROOT / "seeds" / "venues_nfl.csv").open()))
    assert len(rows_) == 67 and len({r["nflverse_id"] for r in rows_}) == 67
    for r in rows_:
        lat_min, lat_max, lon_min, lon_max = BOXES[r["country"]]
        assert lat_min <= float(r["lat"]) <= lat_max, r
        assert lon_min <= float(r["lon"]) <= lon_max, r
        assert r["roof_type"] in ("open", "dome", "retractable"), r


def test_nflverse_games_get_venues_and_roof(nfl_week, engine):
    den = venue_of(engine, "2026_06_SEA_DEN")
    assert (den["name"], den["roof_type"], den["roof"]) == (
        "Empower Field at Mile High",
        "open",
        "outdoors",
    )
    london = venue_of(engine, "2026_06_HOU_JAX")
    assert (london["country"], london["lat"]) == ("GB", 51.556)
    atl = venue_of(engine, "2026_06_CHI_ATL")
    assert atl["roof_type"] == "retractable" and atl["roof"] is None  # roof state not yet known


def test_cfbd_venues_from_teams_games_and_venue_list(engine, settings, fake_api, clock):
    cfbd = CfbdSource(engine, settings.source("cfbd"), transport=fake_api.transport, clock=clock)
    for job in ("teams_football", "games_football", "venues_football"):
        fake_api.add(body=fixture_bytes("cfbd", f"{job}.json"))
        assert cfbd.run(job).status == "ok", job
    with engine.connect() as conn:
        got = {
            r.name: r
            for r in conn.execute(
                select(venues.c.name, venues.c.lat, venues.c.roof_type, venues.c.elevation_m,
                       venues.c.tz).join(games, games.c.venue_id == venues.c.venue_id)
            )
        }  # fmt: skip
    assert got["Bryant-Denny Stadium"].lat == 33.208
    assert got["Bryant-Denny Stadium"].tz == "America/Chicago"
    assert got["Bryant-Denny Stadium"].elevation_m == 68.0
    # Neutral-site game: the venue came in with only a name; /venues filled in the rest
    assert got["Mercedes-Benz Stadium"].lat == 33.755
    assert got["Mercedes-Benz Stadium"].roof_type == "dome"


# --- planning ----------------------------------------------------------------------


def test_request_cost_is_weighted_like_open_meteo():
    plan = RequestPlan("observed", [(1, 1)] * 10, "2024-01-01", "2024-01-30")
    assert plan.cost == 10 * 3  # 10 locations x three 2-week blocks


def test_archive_requests_group_games_by_date():
    t = lambda gid, day, lat: Target(gid, datetime(2025, 9, day, 17, tzinfo=UTC), lat, -80.0)  # noqa: E731
    plans = plan_requests([t(1, 7, 40.0), t(2, 7, 41.0), t(3, 14, 40.0)], "observed")
    assert [(p.start_date, len(p.locations), p.cost) for p in plans] == [
        ("2025-09-07", 2, 2), ("2025-09-14", 1, 1)]  # fmt: skip


# --- forecasts and observed weather --------------------------------------------------


def test_forecast_for_outdoor_games_in_the_next_week(nfl_week, weather, engine, meteo):
    result = weather.run("forecast", sport="nfl")
    assert result.status == "ok", result.message
    assert len(meteo.requests) == 1  # three venues in one request
    q = meteo.requests[0].url
    assert q.host == "api.open-meteo.com" and q.path == "/v1/forecast"
    params = dict(q.params)
    assert params["timezone"] == "GMT" and params["wind_speed_unit"] == "kmh"
    assert params["hourly"].split(",") == [
        "temperature_2m",
        "relative_humidity_2m",
        "precipitation",
        "wind_speed_10m",
        "wind_gusts_10m",
        "wind_direction_10m",
    ]
    assert len(params["latitude"].split(",")) == 3  # DEN, London, ATL (retractable)
    got = rows(engine)
    assert len(got) == 3 * 4 and result.rows_seen == 3
    den = [r for r in got if r["valid_time"].hour == 0 and r["valid_time"].day == 16]
    assert den and den[0]["wind_kph"] == 0.0 and den[0]["kind"] == "forecast"
    assert den[0]["lead_hours"] == pytest.approx(10.25)  # 14:00Z -> 00:15Z kickoff
    with engine.connect() as conn:  # grid elevation filled in for Denver
        elev = conn.execute(
            select(venues.c.elevation_m).where(venues.c.name.like("Empower%"))
        ).scalar()
    assert elev == 1609.0


def test_forecasts_accumulate_and_replay_is_a_noop(nfl_week, weather, engine, clock):
    weather.run("forecast", sport="nfl")
    clock.advance(hours=3)
    weather.run("forecast", sport="nfl")
    assert len(rows(engine)) == 24  # both forecasts kept
    with engine.connect() as conn:
        raw_id = conn.execute(
            select(func.min(raw_responses.c.raw_id)).where(raw_responses.c.source == "open_meteo")
        ).scalar()
    assert weather.reparse(raw_id) == (0, 3)


def test_domes_are_skipped(nfl_week, weather, engine, meteo):
    with engine.begin() as conn:
        conn.execute(update(venues).where(venues.c.name.like("Empower%")).values(roof_type="dome"))
    weather.run("forecast", sport="nfl")
    assert len(meteo.requests[0].url.params["latitude"].split(",")) == 2


def test_observed_after_archive_delay(nfl_week, weather, engine, meteo, clock):
    clock.advance(days=10)
    result = weather.run("observed", sport="nfl")
    assert result.status == "ok", result.message
    hosts = {r.url.host for r in meteo.requests}
    assert hosts == {"archive-api.open-meteo.com"}
    assert len(meteo.requests) == 2  # Oct 16 (DEN); Oct 18 (London and Atlanta share a request)
    observed = rows(engine, weather_observations.c.kind == "observed")
    assert len(observed) == 12 and all(r["lead_hours"] is None for r in observed)
    again = weather.run("observed", sport="nfl")
    assert again.status == "skipped" and "no outdoor nfl games" in again.message


def test_history_backfill_by_season(nfl_week, weather, engine, meteo):
    result = weather.run("observed", sport="nfl", seasons="2024-2025")
    assert result.status == "ok"
    # 2024 Super Bowl (Superdome: dome, skipped), 2025 DAL@PHI (outdoors), KC@LAC (SoFi: dome)
    assert result.rows_seen == 1


def test_non_gmt_response_is_rejected(nfl_week, engine, settings, clock):
    bad = OpenMeteoSource(
        engine,
        settings.source("open_meteo"),
        transport=httpx.MockTransport(FakeOpenMeteo(offset=-14400)),
        clock=clock,
    )
    result = bad.run("forecast", sport="nfl")
    assert result.status == "failed" and "GMT" in result.message
    assert rows(engine) == []


def test_nothing_to_do_makes_no_request(weather, meteo):
    assert weather.run("forecast", sport="ncaaf").status == "skipped"
    assert meteo.requests == []


def test_quota_counts_weighted_cost(nfl_week, weather):
    weather.run("forecast", sport="nfl")
    assert weather.quota.status().used_effective == 3


def test_request_archive_meta_kept_but_not_sent(nfl_week, weather, engine, meteo):
    weather.run("forecast", sport="nfl")
    assert "_meta" not in meteo.requests[0].url.params
    with engine.connect() as conn:
        params = conn.execute(
            select(raw_responses.c.params).where(raw_responses.c.source == "open_meteo")
        ).scalar()
    assert params["_meta"]["kind"] == "forecast" and len(params["_meta"]["games"]) == 3
    json.dumps(params)  # stored as plain JSON
