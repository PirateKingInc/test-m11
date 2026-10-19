"""M13 weather: forecast lead per point, observed stand-in, archived-forecast job, the `wx`
group's as-of reads, and the evaluation."""

import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import select

from betdata.db.schema import game_source_ids, nfl_weather_asof
from betdata.nfl import weather as nw
from betdata.nfl.features.weather import Weather
from betdata.nfl.points import FINAL_REPORT, PREKICK, final_report_time, prekick_time
from betdata.nfl.store import AsOf, Frame
from betdata.sources.nflverse import NflverseSource
from betdata.sources.open_meteo import OpenMeteoSource, hist_plans
from tests.conftest import fixture_bytes

SUN_1PM = datetime(2025, 9, 7, 17, tzinfo=UTC)  # 1:00pm EDT
THU_815 = datetime(2025, 9, 12, 0, 15, tzinfo=UTC)  # Thursday 8:15pm EDT


def test_lead_days_put_the_run_before_the_point():
    assert nw.lead_days(SUN_1PM, PREKICK) == 1
    assert nw.lead_days(SUN_1PM, FINAL_REPORT) == 3  # report Friday 5pm; last hour Sun 4pm
    assert nw.lead_days(THU_815, FINAL_REPORT) == 2  # report Wednesday 5pm
    for kick in (SUN_1PM, THU_815):
        for point in (FINAL_REPORT, PREKICK):
            n = nw.lead_days(kick, point)
            last_hour = nw.kickoff_hour(kick) + timedelta(hours=nw.GAME_HOURS - 1)
            issued = last_hour - timedelta(days=n)
            assert issued + nw.MODEL_LATENCY <= nw.point_time(point, kick)


def _hourly(start: datetime, hours: int, suffixes=("",), base=10.0) -> dict:
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(hours)]
    out = {"time": times}
    for s in suffixes:
        k = int(s[-1]) if s else 0
        out[f"temperature_2m{s}"] = [base + k] * hours
        out[f"wind_speed_10m{s}"] = [20.0 + k] * hours
        out[f"wind_gusts_10m{s}"] = [30.0] * hours
        out[f"precipitation{s}"] = [0.5] * hours
    return {"utc_offset_seconds": 0, "hourly": out}


def test_forecast_rows_pick_the_lead_per_point():
    start = SUN_1PM - timedelta(hours=5)
    hf = _hourly(start, 12)
    prev = _hourly(start, 12, suffixes=("_previous_day1", "_previous_day2", "_previous_day3"))
    now = datetime(2026, 10, 20, tzinfo=UTC)
    rows = nw.forecast_rows(5, SUN_1PM, hf, prev, now, None)
    by = {(r["point"], r["source"]): r for r in rows}
    assert by[(PREKICK, "open_meteo_prev")]["lead_days"] == 1
    assert by[(PREKICK, "open_meteo_prev")]["temp_c"] == 11.0  # day1 values
    assert by[(FINAL_REPORT, "open_meteo_prev")]["temp_c"] == 13.0  # day3 values
    assert by[(FINAL_REPORT, "open_meteo_hf")]["lead_days"] is None
    assert by[(PREKICK, "open_meteo_hf")]["precip_mm"] == pytest.approx(2.0)  # 4 hours summed
    assert by[(PREKICK, "open_meteo_hf")]["known_at"] == prekick_time(SUN_1PM)
    with pytest.raises(ValueError):
        nw.from_response({"utc_offset_seconds": 3600, "hourly": {"time": []}}, SUN_1PM)


@pytest.fixture
def loaded(engine, settings, fake_api, clock):
    nfl = NflverseSource(engine, settings.source("nflverse"), transport=fake_api.transport,
                         sleep=lambda s: None, clock=clock)  # fmt: skip
    fake_api.add(body=fixture_bytes("nflverse", "games_sample.csv"))
    assert nfl.run("schedules", seasons="all").status == "ok"
    with engine.connect() as conn:
        gid = conn.execute(select(game_source_ids.c.game_id).where(
            game_source_ids.c.source_game_id == "2025_01_DAL_PHI")).scalar()  # fmt: skip
    return gid


def test_observed_stand_in_from_archived_games_csv(engine, loaded):
    with engine.begin() as conn:
        assert nw.load_observed(conn, last_season=2025) == 2  # one outdoor game, two points
        assert nw.load_observed(conn, last_season=2025) == 0  # idempotent
        rows = conn.execute(select(nfl_weather_asof)).mappings().all()
    r = {x["point"]: x for x in rows}
    assert r[PREKICK]["game_id"] == loaded and r[PREKICK]["source"] == "nflverse_obs"
    assert r[PREKICK]["temp_c"] == pytest.approx((75 - 32) * 5 / 9, abs=0.01)
    assert r[PREKICK]["wind_kph"] == pytest.approx(11 * 1.609344, abs=0.01)
    with engine.begin() as conn:  # the default stops at 2021: nothing from 2025
        conn.execute(nfl_weather_asof.delete())
        assert nw.load_observed(conn) == 0


def test_archived_forecast_job(engine, settings, fake_api, clock, loaded):
    src = OpenMeteoSource(engine, settings.source("open_meteo"), transport=fake_api.transport,
                          sleep=lambda s: None, clock=clock)  # fmt: skip
    targets = src.hist_targets({"seasons": "2025"})
    assert [t.game_id for t in targets] == [loaded]  # the only finished outdoor game
    [plan] = hist_plans(targets)
    kick = targets[0].kickoff
    start = nw.kickoff_hour(kick) - timedelta(hours=2)
    fake_api.add(body=json.dumps(_hourly(start, 10)))
    fake_api.add(body=json.dumps(_hourly(start, 10, suffixes=(
        "_previous_day1", "_previous_day2", "_previous_day3"))))  # fmt: skip
    res = src.run("hist_forecast", seasons="2025")
    assert (res.status, res.rows_inserted) == ("ok", 4), res.message
    hosts = [r.url.host for r in fake_api.requests[-2:]]
    assert hosts == ["historical-forecast-api.open-meteo.com", "previous-runs-api.open-meteo.com"]
    assert "temperature_2m_previous_day3" in str(fake_api.requests[-1].url)
    assert src.hist_targets({"seasons": "2025"}) == []  # done: not fetched again
    assert src.run("hist_forecast", seasons="2025").status == "skipped"
    assert plan.cost >= 1


def _store_with(rows, kick):
    from tests.nfl.synth import make_store

    store = make_store()
    gid = int(store.games.index[-1])
    store.games.at[gid, "kickoff"] = kick
    df = pd.DataFrame([{"game_id": gid, **r} for r in rows])
    store.frames["weather_asof"] = Frame(df)
    live = store.frames["weather"].df
    store.frames["weather"] = Frame(live[live.game_id != gid])  # no live forecasts for it
    return store, gid


def test_wx_group_reads_only_what_was_known():
    kick = pd.Timestamp("2025-12-07 18:00", tz="UTC")
    fr, pk = final_report_time(kick), prekick_time(kick)
    rows = [
        dict(point=FINAL_REPORT, source="nflverse_obs", temp_c=-5.0, wind_kph=40.0, gust_kph=None,
             precip_mm=None, known_at=fr),
        dict(point=FINAL_REPORT, source="open_meteo_prev", temp_c=0.0, wind_kph=30.0,
             gust_kph=40.0, precip_mm=1.0, known_at=fr),
        dict(point=PREKICK, source="open_meteo_prev", temp_c=2.0, wind_kph=12.0, gust_kph=20.0,
             precip_mm=0.0, known_at=pk),
    ]  # fmt: skip
    store, gid = _store_with(rows, kick)
    g = store.games.loc[gid]
    g = g.copy()
    g["roof_type"], g["surface"] = "outdoors", "grass"
    g["game_id"] = gid
    wx = Weather()
    early = wx.compute(AsOf(store, fr - timedelta(minutes=1)), g, FINAL_REPORT)
    assert early["t_wx_wind"] == 0.0  # nothing known yet (synth has no live forecasts)
    at_report = wx.compute(AsOf(store, fr), g, FINAL_REPORT)
    assert at_report["t_wx_wind"] == pytest.approx(20.0)  # the forecast beats the stand-in
    assert at_report["t_wx_strong"] == pytest.approx(5.0)
    assert at_report["t_wx_cold"] == pytest.approx(5.0) and at_report["t_wx_rain"] == 1.0
    at_kick = wx.compute(AsOf(store, pk), g, PREKICK)
    assert at_kick["t_wx_wind"] == pytest.approx(2.0)  # the prekick row, not the report's
    g["roof_type"] = "dome"
    assert all(v == 0 for v in wx.compute(AsOf(store, pk), g, PREKICK).values())


def test_wx_not_in_m12_groups():
    from betdata.nfl.groups import GROUPS, ORDER
    from betdata.nfl.selection import Design

    assert "wx" in GROUPS and "wx" not in ORDER
    assert "wx" not in Design.load().groups  # the frozen M12 design is untouched


def test_evaluation_keeps_wx_only_where_it_helps():
    from betdata.nfl.features.availability import learn_play_prob
    from betdata.nfl.groups import GROUPS
    from betdata.nfl.pipeline import build, outcomes
    from betdata.nfl.selection import Design
    from tests.nfl.synth import make_store

    design = Design(["base", "avail", "str"], ["base", "avail", "env"], 100.0, 30.0)
    store = make_store()
    store = store.with_params(**learn_play_prob(store, [2018]))
    rows = build(store, [GROUPS[g] for g in [*design.groups, "wx"]], list(store.games.index))
    report, new = nw.evaluate_weather(rows, outcomes(store), design)
    assert ("wx" in new.margin_groups) == report["keep_margin"]
    assert ("wx" in new.total_groups) == report["keep_total"]
    assert new.lam_margin == design.lam_margin and new.blend_total == design.blend_total


def test_forecast_vs_observed_diagnostic(engine, loaded):
    with engine.begin() as conn:
        assert nw.forecast_vs_observed(conn, first_season=2025) == {"games": 0}
        kick = datetime(2025, 9, 5, 0, 20, tzinfo=UTC)
        nw.insert_new(conn, [dict(game_id=loaded, point=PREKICK, source="open_meteo_prev",
                                  lead_days=1, temp_c=25.0, wind_kph=20.0, gust_kph=None,
                                  precip_mm=0.0, known_at=prekick_time(kick),
                                  fetched_at=kick, raw_id=None)])  # fmt: skip
        d = nw.forecast_vs_observed(conn, first_season=2025)
    assert d["games"] == 1  # DAL @ PHI 2025: reported 75 F, 11 mph
    assert d["temp_c_bias"] == pytest.approx(25.0 - (75 - 32) * 5 / 9)
    assert d["wind_kph_bias"] == pytest.approx(20.0 - 11 * 1.609344)
