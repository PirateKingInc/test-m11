"""M15 probe 2: prop-line coverage per season, item structure, lineup sources."""
import io, json, os, time, urllib.request, urllib.error
from collections import Counter
from datetime import date, timedelta

TOK = os.environ.get("GH_TOKEN", "")
def get(url, gh=False, raw=False):
    h = {"User-Agent": "betdata-probe"}
    if gh and TOK: h["Authorization"] = f"Bearer {TOK}"
    time.sleep(0.6)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
            b = r.read(); return r.status, (b if raw else json.loads(b))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return str(e)[:80], None

C = "https://sports.core.api.espn.com/v2/sports"
SITE = "https://site.api.espn.com/apis/site/v2/sports"
WANT_NBA = {"Total Points", "Total Rebounds", "Total Assists", "Total 3-Point Field Goals",
            "Total Points, Rebounds, and Assists"}
WANT_NFL = {"Total Passing Yards (incl. overtime)", "Total Rushing Yards (incl. overtime)",
            "Total Receiving Yards (incl. overtime)", "Total Receptions (incl. overtime)",
            "Anytime Touchdown Scorer"}

def game_props(sport, league, eid, want):
    st, d = get(f"{C}/{sport}/leagues/{league}/events/{eid}/competitions/{eid}/odds?lang=en&region=us")
    res = {}
    for i in (d or {}).get("items", []):
        pid = (i.get("provider") or {}).get("id")
        if not pid or int(pid) > 999: continue
        st, pb = get(f"{C}/{sport}/leagues/{league}/events/{eid}/competitions/{eid}/odds/{pid}/propBets?lang=en&region=us&limit=1000")
        if pb and pb.get("items"):
            c = Counter((x.get("type") or {}).get("name") for x in pb["items"])
            res[pid] = {k: v for k, v in c.items() if k in want}
    return res

def scan(sport, league, dates, want):
    for dt in dates:
        st, d = get(f"{SITE}/{sport}/{league}/scoreboard?dates={dt:%Y%m%d}")
        ev = [e["id"] for e in (d or {}).get("events", [])][:2]
        for eid in ev:
            print(f"{league} {dt} {eid} {game_props(sport, league, eid, want)}", flush=True)

def months(y0, m0, n):
    out = []
    for k in range(n):
        y, m = y0 + (m0 - 1 + k) // 12, (m0 - 1 + k) % 12 + 1
        out.append(date(y, m, 15))
    return out

print("# NBA coverage by month")
for season in range(2021, 2026):
    scan("basketball", "nba", [d for d in months(season, 10, 9) if not (d.month in (7, 8, 9))][1:8], WANT_NBA)
print("# NFL coverage by month")
for season in range(2022, 2026):
    scan("football", "nfl", [date(season, 9, 18), date(season, 10, 16), date(season, 11, 20), date(season, 12, 18)], WANT_NFL)

print("# structure: one athlete, current game")
st, d = get(f"{SITE}/basketball/nba/scoreboard?dates=20261022")
eid = d["events"][0]["id"]
st, pb = get(f"{C}/basketball/leagues/nba/events/{eid}/competitions/{eid}/odds/100/propBets?lang=en&region=us&limit=1000")
items = pb["items"]
a0 = items[0]["athlete"]["$ref"]
for x in items:
    if x.get("athlete", {}).get("$ref") == a0 and x["type"]["name"] in ("Total Points", "Points Milestones"):
        print(json.dumps({k: v for k, v in x.items() if k not in ("competition", "provider")}))
print("# structure: finished 2024-25 game provider 58, one athlete")
st, pb = get(f"{C}/basketball/leagues/nba/events/401705426/competitions/401705426/odds/58/propBets?lang=en&region=us&limit=1000")
a0 = pb["items"][0]["athlete"]["$ref"]
for x in pb["items"]:
    if x.get("athlete", {}).get("$ref") == a0:
        print(json.dumps({k: v for k, v in x.items() if k not in ("competition", "provider")}))
print("# opening night game still has props after the game?")
st, d = get(f"{C}/basketball/leagues/nba/events/401909088/competitions/401909088/odds?lang=en&region=us")
print(st, [(i.get("provider") or {}).get("id") for i in (d or {}).get("items", [])])
for pid in ("100", "58"):
    st, pb = get(f"{C}/basketball/leagues/nba/events/401909088/competitions/401909088/odds/{pid}/propBets?lang=en&region=us&limit=1000")
    print(pid, st, pb and pb.get("count"))
print("# pagination check")
print(pb and {k: pb.get(k) for k in ("count", "pageIndex", "pageSize", "pageCount")})
print("# NFL structure: anytime TD + receiving yards, finished 2024 game")
st, pb = get(f"{C}/football/leagues/nfl/events/401671801/competitions/401671801/odds/58/propBets?lang=en&region=us&limit=1000")
seen = Counter()
for x in pb["items"]:
    t = x["type"]["name"]
    if t in WANT_NFL and seen[t] < 3:
        seen[t] += 1
        print(json.dumps({k: v for k, v in x.items() if k not in ("competition", "provider")}))
print("# ESPN NBA summary plays (substitutions)")
st, s = get(f"{SITE}/basketball/nba/summary?event=401909088")
plays = s.get("plays", [])
print(len(plays), [p for p in plays if "enters" in p.get("text", "")][:2])
print(s["boxscore"]["players"][0]["statistics"][0].get("athletes", [{}])[0])
print("# shufinskiy pbpstats files")
st, tree = get("https://api.github.com/repos/shufinskiy/nba_data/git/trees/main?recursive=1", gh=True)
print([t["path"] for t in (tree or {}).get("tree", []) if "pbpstats" in t["path"] or "nbastats_20" in t["path"]])
