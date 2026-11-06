"""M16 lag-study decision (record only): the poller's Signal 1 decision and the lag summary."""
import sqlite3
import subprocess

db = sqlite3.connect("data/poller.sqlite")
for k in ("signal1:decision", "signal1:off", "nfl_value:decision", "nfl_value:off"):
    print(k, db.execute("select value, updated_at from poller_state where key=?", (k,)).fetchall())
print("last runs", db.execute("select run_id, started_at, status, message from poller_runs "
                              "order by run_id desc limit 3").fetchall())
r = subprocess.run(["uv", "run", "--no-dev", "betdata", "alerts", "lag"], capture_output=True,
                   text=True, timeout=600)
print("$ betdata alerts lag rc", r.returncode)
print(r.stdout[-4000:], r.stderr[-1500:])
