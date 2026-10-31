"""First-card check: the poller's card state and its recent jobs. Counts and fields only."""
import sqlite3

db = sqlite3.connect("data/poller.sqlite")
print("card state", db.execute("select key, substr(value,1,600), updated_at from poller_state "
                               "where key like 'card:%'").fetchall())
print("due/tracker", db.execute("select key, value from poller_state where key like 'due:%'"
                                ).fetchall())
for r in db.execute("select run_id, started_at, finished_at, status, cycles, message from "
                    "poller_runs order by run_id desc limit 6"):
    print("run", r)
m = sqlite3.connect("data/betdata.sqlite")
print("nba games today", m.execute(
    "select count(*), min(start_time_utc) from games g join sports s on s.sport_id=g.sport_id "
    "where s.key='nba' and start_time_utc >= '2026-10-31 04:00' and start_time_utc < "
    "'2026-11-01 04:00'").fetchall())
print("nba alerts today", db.execute(
    "select count(*), min(created_at), max(created_at) from alerts where sport='nba' and "
    "tip >= '2026-10-31 04:00' and tip < '2026-11-01 04:00'").fetchall())
