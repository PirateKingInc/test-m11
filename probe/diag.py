import json, sqlite3
db = sqlite3.connect("data/betdata.sqlite")
print("== nba predictions by predicted_at")
for r in db.execute("select predicted_at, prediction_point, count(*) from model_predictions where model='nba' and predicted_at >= '2026-10-21' group by 1,2 order by 1"):
    print(r)
print("== runner runs today")
for r in db.execute("select run_id, trigger, started_at, finished_at, status, details from runner_runs where started_at >= '2026-10-21 10:00' order by run_id"):
    d = json.loads(r[5] or "{}")
    print(r[:5], json.dumps(d.get("sports")))
print("== prop tables", db.execute("select count(*) from prop_lines").fetchall(), db.execute("select count(*) from prop_predictions").fetchall())
