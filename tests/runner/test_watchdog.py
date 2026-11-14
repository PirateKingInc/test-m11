"""The hourly watchdog and the clock's catch-up (owner 2026-11-14, PROJECT.md "Hourly
reliability")."""

from datetime import UTC, datetime, timedelta

import yaml

from betdata.config import PROJECT_ROOT
from betdata.runner import watchdog as W
from betdata.runner import window

# Sat Nov 14 2026, EST (UTC-5): 7 AM ET = 12:00 UTC
T7 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def _clock(now):  # a healthy clock run, started at the top of the hour
    return [now.replace(minute=0, second=8)]


def test_quiet_when_hourly_runs_complete():
    now = T7 + timedelta(hours=3, minutes=27)  # 10:27 AM
    runs = W.Runs(hourly_done=[T7 + timedelta(hours=3, minutes=3)], clock_active=_clock(now))
    d = W.decide(now, runs, {})
    assert not d.dispatch_clock and d.dispatch_hourly is None and d.alert is None


def test_fires_on_a_90_minute_gap_once_then_still_down_after_45_minutes():
    last = T7 + timedelta(hours=2, minutes=3)  # the 9 AM run, then nothing
    runs = W.Runs(hourly_done=[last], clock_active=[])
    st: dict = {}
    seen = []
    for m in range(0, 241, 20):  # every 20 minutes from 9:03 AM for 4 hours
        now = last + timedelta(minutes=m)
        d = W.decide(now, runs, st)
        st = d.state
        seen.append((m, d.dispatch_hourly, d.alert))
    alerts = [(m, a) for m, _h, a in seen if a]
    hourly = [(m, h) for m, h, _a in seen if h]
    # missed: 10 AM was due; the gap passes 90 min at 10:33 -> the 10:43 check (m=100)
    assert alerts[0] == (100, "betdata: hourly run missed since 10:00 AM ET — restarted")
    assert hourly == [(100, "watchdog")]  # dispatched once
    # still down only once, and only after 45 minutes from the restart
    assert alerts[1:] == [(160, "betdata: hourly run still down since 10:00 AM ET")]
    assert len(alerts) == 2


def test_no_alert_before_90_minutes_and_recovery_clears_the_outage():
    last = T7 + timedelta(hours=2, minutes=3)
    now = last + timedelta(minutes=85)
    assert W.decide(now, W.Runs([last], False, _clock(now)), {}).alert is None
    d = W.decide(last + timedelta(minutes=100), W.Runs([last], False, []), {})
    assert d.alert and d.state.get("outage")
    back = last + timedelta(minutes=130)
    d2 = W.decide(back + timedelta(minutes=5), W.Runs([last, back], False, _clock(back)),
                  d.state)  # fmt: skip
    assert "outage" not in d2.state and d2.alert is None
    # a new outage later alerts again (one alert per outage)
    d3 = W.decide(back + timedelta(minutes=120), W.Runs([last, back], False, []), d2.state)
    assert d3.alert and "missed since" in d3.alert


def test_silent_overnight_but_keeps_the_clock_alive():
    last = datetime(2026, 11, 15, 3, 3, tzinfo=UTC)  # 10:03 PM ET Nov 14
    for h in (4, 6, 8, 10):  # 11 PM - 5 AM ET
        now = last + timedelta(hours=h - 3, minutes=24)
        d = W.decide(now, W.Runs([last], False, []), {})
        assert d.alert is None and d.dispatch_hourly is None
        assert d.dispatch_clock  # no clock running: restarted silently
    now = last + timedelta(hours=5)
    d = W.decide(now, W.Runs([last], False, _clock(now)), {})
    assert not d.dispatch_clock and d.alert is None


def test_morning_slot_and_6am_no_false_alarm():
    last = datetime(2026, 11, 15, 3, 3, tzinfo=UTC)  # 10:03 PM ET
    six = datetime(2026, 11, 15, 11, 7, tzinfo=UTC)  # 6:07 AM ET: nothing due yet
    assert W.decide(six, W.Runs([last], False, _clock(six)), {}).alert is None
    late = datetime(2026, 11, 15, 11, 47, tzinfo=UTC)  # 6:47 AM ET, no morning run
    d = W.decide(late, W.Runs([last], False, _clock(late)), {})
    assert d.dispatch_hourly == "morning" and "6:15 AM ET" in d.alert
    # an hourly run in progress: wait, no alert
    assert W.decide(late, W.Runs([last], True, _clock(late)), {}).alert is None


def test_hung_clock_is_restarted():
    now = T7 + timedelta(minutes=27)
    hung = [T7 - timedelta(minutes=60)]  # started 87 minutes ago and still "running"
    d = W.decide(now, W.Runs([T7 + timedelta(minutes=3)], False, hung), {})
    assert d.dispatch_clock and d.note == "clock: hung" and d.alert is None


def test_catchup_only_on_a_mid_hour_restart_inside_the_window():
    assert not window.catchup_due(T7 + timedelta(seconds=8))  # a normal tick
    assert window.catchup_due(T7 + timedelta(minutes=11))  # restarted at 7:11 AM
    assert not window.catchup_due(T7 - timedelta(minutes=30))  # 6:30 AM: not an hourly slot
    assert not window.catchup_due(datetime(2026, 11, 15, 5, 20, tzinfo=UTC))  # 12:20 AM


def test_workflows_wired():
    wf = PROJECT_ROOT / ".github" / "workflows"
    clock = yaml.safe_load((wf / "clock.yml").read_text())
    assert "schedule" not in clock[True]  # no self-cancelling restart cron
    tick = clock["jobs"]["tick"]
    assert tick["timeout-minutes"] <= 66
    names = [s.get("name", "") for s in tick["steps"]]
    assert names.index("Catch up this hour (a mid-hour restart only)") < names.index(
        "Wait for the next hour")
    wait = next(s for s in tick["steps"] if s.get("name") == "Wait for the next hour")
    assert wait["timeout-minutes"] < tick["timeout-minutes"]
    dog = yaml.safe_load((wf / "watchdog.yml").read_text())
    assert dog[True]["schedule"][0]["cron"] == "7,27,47 * * * *"
    assert "runner/watchdog.py" in dog["jobs"]["check"]["steps"][2]["run"]
    from betdata.runner.hourly import AUTOMATIC_TRIGGERS

    assert "watchdog" in AUTOMATIC_TRIGGERS  # a restart never runs a slot twice
