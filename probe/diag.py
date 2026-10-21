import sqlite3, json, gzip
c = sqlite3.connect("data/betdata.sqlite")
r = c.execute("select body_gz from raw_responses where source='espn_nba' and endpoint='/scoreboard' order by raw_id desc limit 1").fetchone()
d = json.loads(gzip.decompress(r[0]))
for e in d["events"]:
    comp = e["competitions"][0]
    if e["status"]["type"]["name"] == "STATUS_SCHEDULED":
        print(e["id"], e["date"], "odds:", json.dumps(comp.get("odds"))[:700]); break
print("n events", len(d["events"]), [ (e["id"], e["status"]["type"]["name"], bool(e["competitions"][0].get("odds"))) for e in d["events"]])
from datetime import datetime, UTC
from betdata.config import get_settings
from betdata.db.engine import make_engine
from betdata.nba.live import Design, load_fit, loaded_store
from betdata.nba.features import team_status, _state
from betdata.nfl.store import AsOf
e = make_engine(get_settings().db_url)
fits, play, version = load_fit(Design.load())
now = datetime(2026, 10, 21, 2, 30, tzinfo=UTC)
st = loaded_store(e, now, play)
g = st.games.loc[22881]
a = AsOf(st, now)
for team in (g.home, g.away):
    s, rep = team_status(a, g, team)
    rot = _state(a).rotation(team)
    print("team", team, "reported", rep, "statuses", list(s.items())[:8])
    print("  rotation", [(pid, st.names.get(pid), round(p.min_play,1), p.streak) for pid, p in rot][:12])
    print("  listed in rotation:", [pid for pid in s if pid in dict(rot)])
inj = st.frame("injuries").df
print(inj[inj.game_id == 22881].head(12).to_string())
print("state season", _state(a).season)
