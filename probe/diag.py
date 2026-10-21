import json, sqlite3
db = sqlite3.connect("data/betdata.sqlite")
q = lambda s: db.execute(s).fetchall()
print("prop_predictions by hour", q("select model, substr(predicted_at,1,13), count(*) from prop_predictions group by 1,2 order by 2"))
print("models by hour", q("select model, substr(predicted_at,1,13), count(*) from model_predictions where predicted_at >= '2026-10-21 21:30' group by 1,2 order by 2"))
for r in q("select run_id, trigger, started_at, status, details from runner_runs order by run_id desc limit 3"):
    d = json.loads(r[4] or "{}")
    print(r[:4], d.get("seconds"), {k: v.get("predict_seconds") for k, v in (d.get("sports") or {}).items()},
          [(j["id"], j.get("seconds")) for j in d.get("jobs", []) if j.get("seconds")])
print("prop_lines 24h", q("select source, count(*) from prop_lines where fetched_at >= '2026-10-21' group by 1"))
