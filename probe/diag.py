import sqlite3, json
c = sqlite3.connect("data/betdata.sqlite")
q = lambda s: c.execute(s).fetchall()
print("odds by source/kind", q("select source, snapshot_kind, count(*), max(fetched_at) from odds_snapshots where source in ('espn_nba') group by 1,2"))
print("espn ingest", q("select run_id, job, status, rows_inserted, rows_seen, message, started_at from ingest_runs where source='espn_nba' order by run_id desc limit 6"))
print("nba games tomorrow", q("select g.game_id, g.start_time_utc, g.status, (select group_concat(source||':'||source_game_id) from game_source_ids s where s.game_id=g.game_id) from games g join sports sp on sp.sport_id=g.sport_id and sp.key='nba' where g.start_time_utc between '2026-10-21 12:00' and '2026-10-22 06:00' order by 2 limit 6"))
print("unlinked odds", q("select count(*) from odds_snapshots where source='espn_nba' and game_id is null"))
print("source_events espn_nba", q("select source_game_id, home_name, away_name, start_time_utc from source_events where source='espn_nba' limit 6"))
print("injury rows", q("select count(*), sum(game_id is null), sum(team_id is null), min(game_date), max(game_date), max(known_at) from nba_injury_reports"))
print("injury sample", q("select game_date, matchup, game_id, team_name, player_name, status, known_at from nba_injury_reports order by injury_report_id desc limit 8"))
print("raw scoreboard", q("select raw_id, length(body_gz), fetched_at from raw_responses where source='espn_nba' and endpoint='/scoreboard' order by raw_id desc limit 2"))
import gzip
r = c.execute("select body_gz from raw_responses where source='espn_nba' and endpoint='/scoreboard' order by raw_id desc limit 1").fetchone()
if r:
    d = json.loads(gzip.decompress(r[0]))
    for e in d["events"][:3]:
        comp = e["competitions"][0]
        print(e["id"], e["date"], e["status"]["type"]["name"], "odds:", json.dumps(comp.get("odds"))[:400])
print("predictions", q("select game_id, model_version, prediction_point, predicted_at, market_spread_home, substr(factors,1,300) from model_predictions where model='nba' order by prediction_id desc limit 3"))
