"""The hourly watchdog (owner 2026-11-14, PROJECT.md "Hourly reliability"): a scheduled workflow
(.github/workflows/watchdog.yml), independent of the clock chain, run every ~20 minutes.

1. The clock chain (silent, any hour): when no clock run is queued or running, or the running one
   started more than CLOCK_HUNG ago (a lost runner: its "wait for the next hour" never ends), the
   clock is dispatched once (a new clock run cancels a hung one).
2. Missed hourly runs (6:00 AM - 11:00 PM ET only): when no hourly run did its work (the `run`
   job succeeded) in the last 90 minutes although a slot was due (the 6:15 AM morning update,
   or 7 AM - 10 PM ET on the hour) at least GRACE ago, and no hourly run is queued or running,
   the clock and the hourly run are dispatched once and one ntfy line is sent:
   "betdata: hourly run missed since <time ET> — restarted". If no hourly run completes within
   45 minutes of that restart, one more line: "betdata: hourly run still down since <time ET>".
   One alert per outage; the outage ends when an hourly run completes.

Standard library only (it runs before anything is installed). State lives in a small JSON file
kept in the Actions cache between runs.

The watchdog keeps its own time (owner 2026-11-14): GitHub's cron fires rarely under load (on Nov
14 the hourly cron started twice between 21:00 and 23:00 UTC; the watchdog's own cron not once),
so each watchdog run checks CHECKS times, CHECK_EVERY apart, then dispatches the next run. The
clock dispatches a watchdog when none is running, and the watchdog a clock: either chain restarts
the other, and the cron is a third way in. A run that starts while an older one is mid-loop
(started less than HANDOFF ago) exits at once, so there is one chain.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
WINDOW = (time(6, 0), time(23, 0))  # Eastern: alerts and missed-hour restarts
FIRST_HOUR, LAST_HOUR = 7, 22  # Eastern: hourly slots on the hour (runner/window.py)
MORNING = time(6, 15)  # the morning update
GAP = timedelta(minutes=90)  # no completed hourly run for this long...
GRACE = timedelta(minutes=20)  # ...although a slot was due at least this long ago
STILL_DOWN = timedelta(minutes=45)
CLOCK_HUNG = timedelta(minutes=70)  # a clock run waits at most ~61 minutes
LOOKBACK = timedelta(hours=36)  # completed hourly runs are looked up this far back (overnight
# there are none from 10 PM to 6:15 AM ET: 8+ hours)
STATE = Path("data/watchdog.json")
CHECKS = 3
CHECK_EVERY = timedelta(minutes=20)
HANDOFF = timedelta(minutes=45)  # an older run started longer ago than this is hung: ignored
HANDOFF_WAIT = 120  # seconds: an older run that just dispatched this one finishes by then
NTFY = "https://ntfy.sh/"


@dataclass
class Runs:
    """What GitHub reports: completion times of hourly runs whose `run` job succeeded, whether an
    hourly run is queued/running, and the start time of every queued/running clock run."""

    hourly_done: list[datetime] = field(default_factory=list)
    hourly_active: bool = False
    clock_active: list[datetime] = field(default_factory=list)


@dataclass
class Decision:
    dispatch_clock: bool = False
    dispatch_hourly: str | None = None  # the trigger to pass, or None
    alert: str | None = None
    state: dict = field(default_factory=dict)
    note: str = ""


def et(t: datetime) -> str:
    return t.astimezone(EASTERN).strftime("%-I:%M %p ET")


def due_slots(after: datetime, until: datetime) -> list[datetime]:
    """Hourly slots due in (after, until]: the 6:15 AM ET morning update and 7 AM - 10 PM ET on
    the hour (UTC datetimes)."""
    out = []
    d = after.astimezone(EASTERN).date()
    while True:
        day = [datetime.combine(d, MORNING, tzinfo=EASTERN)] + [
            datetime.combine(d, time(h), tzinfo=EASTERN) for h in range(FIRST_HOUR, LAST_HOUR + 1)]
        for s in day:
            s = s.astimezone(UTC)
            if after < s <= until:
                out.append(s)
        if datetime.combine(d, time(0), tzinfo=EASTERN) > until:
            return out
        d += timedelta(days=1)


def in_window(now: datetime) -> bool:
    t = now.astimezone(EASTERN).time()
    return WINDOW[0] <= t < WINDOW[1]


def decide(now: datetime, runs: Runs, state: dict) -> Decision:
    st = dict(state)
    dec = Decision(state=st)
    # 1. the clock chain, silently, at any hour
    alive = [t for t in runs.clock_active if now - t < CLOCK_HUNG]
    clock_ok = bool(alive)
    if not alive:
        dec.dispatch_clock = True
        dec.note = "clock: none running" if not runs.clock_active else "clock: hung"
    last = max(runs.hourly_done, default=None)
    out = st.get("outage")
    if out is not None:  # an outage was reported: has an hourly run completed since?
        restarted = datetime.fromisoformat(out["restarted_at"])
        if last is not None and last > restarted:
            st.pop("outage")
            dec.note = (dec.note + "; " if dec.note else "") + "recovered"
            return dec
        if in_window(now) and not out.get("still_down") and now - restarted >= STILL_DOWN:
            since = datetime.fromisoformat(out["since"])
            dec.alert = f"betdata: hourly run still down since {et(since)}"
            out["still_down"] = True
        return dec
    if not in_window(now) or runs.hourly_active:
        return dec
    if last is None:  # nothing to measure a gap from: never guess (Nov 15 6:14 AM false alarm)
        return dec
    since = last
    if now - since <= GAP:
        return dec
    due = [s for s in due_slots(since, now) if now - s >= GRACE]
    if not due:
        return dec
    first = due[0]
    latest = due[-1]
    is_morning = latest.astimezone(EASTERN).time() == MORNING
    # a healthy clock is never replaced: a new clock run cancels the running one, which on
    # Nov 15 cancelled its wait for the 6:15 morning update
    dec.dispatch_clock = not clock_ok
    dec.dispatch_hourly = "morning" if is_morning else "watchdog"
    dec.alert = f"betdata: hourly run missed since {et(first)} — restarted"
    st["outage"] = {"since": first.isoformat(), "restarted_at": now.isoformat(),
                    "still_down": False}
    dec.note = (dec.note + "; " if dec.note else "") + f"missed since {et(first)}"
    return dec


# --- GitHub and ntfy (the workflow only) -------------------------------------------------------


def _api(path: str, method: str = "GET", body: dict | None = None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json"})  # fmt: skip
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch(now: datetime) -> Runs:
    runs = Runs()
    since = (now - LOOKBACK).strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in _api(f"/actions/workflows/hourly.yml/runs?per_page=100&created=>={since}")[
            "workflow_runs"]:
        if r["status"] in ("queued", "in_progress", "waiting", "pending", "requested"):
            runs.hourly_active = True
            continue
        if r["conclusion"] != "success":
            continue
        for j in _api(f"/actions/runs/{r['id']}/jobs")["jobs"]:
            if j["name"] == "run" and j["conclusion"] == "success" and j.get("completed_at"):
                runs.hourly_done.append(_t(j["completed_at"]))
        if runs.hourly_done:  # newest first: the latest completed run is all the check needs
            break
    for r in _api("/actions/workflows/clock.yml/runs?per_page=10")["workflow_runs"]:
        if r["status"] in ("queued", "in_progress", "waiting", "pending", "requested"):
            runs.clock_active.append(_t(r["run_started_at"] or r["created_at"]))
    return runs


def dispatch(workflow: str, inputs: dict | None = None) -> None:
    _api(f"/actions/workflows/{workflow}/dispatches", "POST",
         {"ref": "main", **({"inputs": inputs} if inputs else {})})


def ntfy(line: str) -> str:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return "no NTFY_TOPIC"
    req = urllib.request.Request(NTFY + topic, data=line.encode(), method="POST",
                                 headers={"Title": "betdata watchdog", "Tags": "warning"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return f"http {r.status}"
    except Exception as e:  # noqa: BLE001 - never fails the check
        return f"error {type(e).__name__}"


def another_chain(now: datetime, me: int, others: list[tuple[int, datetime]]) -> bool:
    """Is an older watchdog run mid-loop (so this one should exit)? `others`: (run id, start)
    of the queued/running watchdog runs."""
    return any(rid < me and now - t < HANDOFF for rid, t in others)


def _others(me: int) -> list[tuple[int, datetime]]:
    out = []
    for r in _api("/actions/workflows/watchdog.yml/runs?per_page=10")["workflow_runs"]:
        if r["id"] != me and r["status"] in ("queued", "in_progress", "waiting", "pending"):
            out.append((r["id"], _t(r["run_started_at"] or r["created_at"])))
    return out


def check() -> None:
    now = datetime.now(UTC)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    dec = decide(now, fetch(now), state)
    if dec.dispatch_clock:
        dispatch("clock.yml")
    if dec.dispatch_hourly:
        dispatch("hourly.yml", {"trigger": dec.dispatch_hourly})
    sent = ntfy(dec.alert) if dec.alert else ""
    dec.state["last_check"] = now.isoformat()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(dec.state))
    clock = "dispatched" if dec.dispatch_clock else "ok"
    print(f"watchdog {now:%Y-%m-%d %H:%M} UTC ({et(now)}): clock={clock}"
          f" hourly={dec.dispatch_hourly or '-'} alert={dec.alert or '-'} {sent} {dec.note}",
          flush=True)


def main(argv: list[str]) -> int:
    import time as _time

    me = int(os.environ.get("GITHUB_RUN_ID", "0"))
    if "--loop" not in argv:
        check()
        return 0
    waited = 0
    while another_chain(datetime.now(UTC), me, _others(me)):
        if waited >= HANDOFF_WAIT:  # still running: mid-loop, not a hand-over
            print("watchdog: an older run is mid-loop; exiting (one chain)")
            _write_out("chain=false")
            return 0
        _time.sleep(15)
        waited += 15
    for i in range(CHECKS):
        check()
        if i < CHECKS - 1:
            _time.sleep(CHECK_EVERY.total_seconds())
    _write_out("chain=true")
    return 0


def _write_out(line: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(line + "\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
