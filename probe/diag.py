"""M18 probe: the 30-day quota proof with every new poll (alerts quota + the hourly plan), and
the live archive / consensus / alert state of the poller database. Counts and fields only."""

import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta

print("now", datetime.now(UTC).isoformat())
for cmd in (["uv", "run", "--no-dev", "betdata", "alerts", "quota", "--days", "30"],
            ["uv", "run", "--no-dev", "betdata", "scheduler", "plan", "--runner", "--days", "30"],
            ["uv", "run", "--no-dev", "betdata", "alerts", "replay-value", "--out",
             "out/m18_replay.json"],
            ["uv", "run", "--no-dev", "betdata", "alerts", "grade"]):
    print("$", " ".join(cmd[3:]))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    print(r.stdout[-6000:])
    if r.returncode:
        print("rc", r.returncode, r.stderr[-2000:])

p = "data/poller.sqlite"
if os.path.exists(p):
    db = sqlite3.connect(p)
    def q(sql, *a):
        return db.execute(sql, a).fetchall()
    tabs = [r[0] for r in q("select name from sqlite_master where type='table'")]
    print("tables", tabs)
    for t in ("quotes", "openers", "poll_marks", "consensus", "consensus_grades", "alerts"):
        if t in tabs:
            print(t, q(f"select count(*) from {t}")[0][0])
    for k in ("sweep:slot", "sweep:log"):
        v = q("select value from poller_state where key=?", k)
        if v:
            print(k, str(v[0][0])[-1200:])
    if "consensus" in tabs:
        print("consensus by market/method", q(
            "select market, method, two_book, count(*) from consensus group by 1,2,3 "
            "order by 4 desc limit 20"))
    print("alerts", q("select alert_id, signal, sport, game_label, market, player_name, side, "
                      "line, price, book, created_at from alerts order by alert_id desc limit 15"))
    print("runs", q("select run_id, started_at, finished_at, status, cycles, requests, message "
                    "from poller_runs order by run_id desc limit 4"))
    if "consensus_grades" in tabs:
        print("graded vs consensus close", q(
            "select a.alert_id, a.signal, a.game_label, a.market, a.player_name, a.side, a.line, "
            "a.price, a.book, round(a.book_prob,4), g.close_method, g.close_two_book, g.close_n, "
            "round(g.close_prob,4), round(g.clv,4), g.close_at, ag.result "
            "from alerts a join consensus_grades g on g.alert_id=a.alert_id "
            "left join alert_grades ag on ag.alert_id=a.alert_id "
            "where a.signal in ('value','snipe','g1','g2') order by a.alert_id"))
        print("value alert reasons", q("select alert_id, reason, details from alerts "
                                       "where signal in ('value','g1','g2') order by alert_id"))
        print("status events since M18", q(
            "select sport, count(*), sum(qualifies) from status_events where detected_at > "
            "'2026-10-29 19:00' group by sport"))
