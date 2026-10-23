"""M16 live check of the poller database: runs, status changes, quotes, alerts, state."""

import os
import sqlite3

p = "data/poller.sqlite"
print("poller db:", os.path.exists(p), os.path.getsize(p) if os.path.exists(p) else None)
c = sqlite3.connect(p)
q = lambda s: c.execute(s).fetchall()  # noqa: E731
print("runs:", q("select run_id, started_at, finished_at, status, cycles, skipped_cycles, "
                 "requests, substr(message,1,200) from poller_runs order by run_id desc limit 6"))
print("events:", q("select count(*), sum(qualifies) from status_events"))
for r in q("select sport, game_id, player_name, old_status, new_status, prob_before, "
           "prob_after, weight, qualifies, report_name, detected_at from status_events "
           "order by event_id desc limit 25"):
    print("  ", r)
print("repreds:", q("select game_id, point, margin_before, margin_after, total_before, "
                    "total_after, created_at from repredictions"))
print("quotes:", q("select source, book, market, count(*), max(polled_at) from quotes "
                   "group by 1,2,3"))
print("alerts:", q("select alert_id, signal, sport, game_label, market, side, line, price, book, "
                   "quiet, sent, send_status, created_at, substr(reason,1,160) from alerts"))
print("state:", q("select key, substr(value,1,150) from poller_state"))
