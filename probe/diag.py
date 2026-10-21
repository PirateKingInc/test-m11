import json, sqlite3
db = sqlite3.connect("data/betdata.sqlite")
q = lambda s: db.execute(s).fetchall()
print("stints by season", q("select g.season, count(*) from nba_stints s join games g on g.game_id=s.game_id group by 1"))
print("prop_lines", q("select source, kind, market, count(*), max(fetched_at) from prop_lines group by 1,2,3"))
print("prop_predictions", q("select model, market, count(*), max(predicted_at) from prop_predictions group by 1,2"))
print("models", q("select model, count(*), max(predicted_at) from model_predictions where predicted_at >= '2026-10-21' group by 1"))
for r in q("select run_id, trigger, started_at, finished_at, status, details from runner_runs order by run_id desc limit 3"):
    d = json.loads(r[5] or "{}"); print(r[:5], d.get("seconds"), json.dumps(d.get("sports")))
    print("   jobs", [(j.get("source"), j.get("job"), j.get("status"), (j.get("message") or "")[:80]) for j in d.get("jobs", []) if "props" in str(j.get("job")) or j.get("source") == "espn_props"])
