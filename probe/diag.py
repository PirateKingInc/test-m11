import json, sqlite3, urllib.request
db = sqlite3.connect("data/betdata.sqlite")
print("== nba games Oct 19-23")
for r in db.execute("""select g.game_id, g.start_time_utc, h.abbrev, a.abbrev, g.status, g.season
  from games g join teams h on h.team_id=g.home_team_id join teams a on a.team_id=g.away_team_id
  join sports s on s.sport_id=g.sport_id where s.key='nba'
  and g.start_time_utc between '2026-10-19' and '2026-10-23' order by 2"""):
    print(r)
print("== source ids for those")
for r in db.execute("""select gs.* from game_source_ids gs where gs.game_id in (select g.game_id from games g
  join sports s on s.sport_id=g.sport_id where s.key='nba' and g.start_time_utc between '2026-10-20' and '2026-10-21 12:00')"""):
    print(r)
print("== espn 20261020")
d = json.load(urllib.request.urlopen("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261020"))
for e in d["events"]:
    print(e["id"], e["date"], e["shortName"], e["status"]["type"]["name"])
print("== nba predictions by predicted_at")
for r in db.execute("select predicted_at, prediction_point, count(*) from model_predictions where model='nba' group by 1,2 order by 1"):
    print(r)
