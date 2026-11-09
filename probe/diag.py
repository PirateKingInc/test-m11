"""Probe (news alerts, read only): the poller database's news log, alerts and lag decision."""
import json
import sqlite3
from collections import Counter

db = sqlite3.connect("file:data/poller.sqlite?mode=ro", uri=True)
db.row_factory = sqlite3.Row


def state(k):
    r = db.execute("select value from poller_state where key=?", (k,)).fetchone()
    return None if r is None else json.loads(r[0])


print("== signal1:decision", json.dumps(state("signal1:decision")))
print("== nfl_value:decision", json.dumps(state("nfl_value:decision")))
tabs = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
print("== early_claims table:", "early_claims" in tabs)
if "early_claims" in tabs:
    rows = db.execute("select * from early_claims order by claim_id").fetchall()
    print("claims:", len(rows))
    print(Counter((r["source"], r["account"], r["outcome"]) for r in rows).most_common())
    for r in rows[-10:]:
        print(" ", r["posted_at"], r["source"], r["account"], r["player_name"], r["status"],
              "ahead" if r["ahead"] else "behind", r["outcome"], r["lead_min"],
              (r["text"] or "")[:90].replace("\n", " "))
print("== flash alerts")
for r in db.execute("select * from alerts where signal='flash' order by alert_id"):
    d = json.loads(r["details"] or "{}")
    print(" ", r["alert_id"], r["created_at"], r["game_label"], r["player_name"], r["market"],
          r["side"], r["line"], r["price"], r["book"], "sent" if r["sent"] else "not sent",
          r["send_status"], d.get("good_down_to"), d.get("minutes_since"), d.get("news_status"),
          d.get("alerts_on"))
    print("    ", (r["reason"] or "").replace("\n", " | "))
print("== reports log (last 15)")
for x in (state("nba:reports") or [])[-15:]:
    print(" ", x)
print("== poller runs with news/sleeper notes (last 40 runs)")
for r in db.execute("select run_id, started_at, finished_at, cycles, status, message, requests "
                    "from poller_runs order by run_id desc limit 40"):
    print(" ", r["run_id"], r["started_at"], r["finished_at"], r["cycles"], r["status"],
          (r["requests"] or "")[:160], "|", (r["message"] or "")[:300])
print("== due keys")
for k in ("due:nba_report", "due:sleeper", "sleeper:counts"):
    v = state(k)
    print(" ", k, (json.dumps(v)[:200] if v is not None else None))
