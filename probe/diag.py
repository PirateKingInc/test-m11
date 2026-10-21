import json, sqlite3
db = sqlite3.connect("data/betdata.sqlite")
q = lambda s: db.execute(s).fetchall()
print("prop_predictions", q("select model, prediction_point, count(*), max(predicted_at) from prop_predictions group by 1,2"))
print("models", q("select model, count(*), max(predicted_at) from model_predictions where predicted_at >= '2026-10-21 21:30' group by 1"))
for r in q("select run_id, trigger, started_at, status, details from runner_runs order by run_id desc limit 2"):
    d = json.loads(r[4] or "{}"); print(r[:4], d.get("seconds"), json.dumps(d.get("sports")))
print("sample", q("select player_name, market, mean, line, p_over, edge, factors from prop_predictions order by predicted_at desc limit 6"))
