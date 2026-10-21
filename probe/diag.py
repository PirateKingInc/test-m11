"""M15 probe 3: raw propBets samples per provider era (saved as files)."""
import json, os, time, urllib.request, urllib.error
C = "https://sports.core.api.espn.com/v2/sports"
SITE = "https://site.api.espn.com/apis/site/v2/sports"
def get(url):
    time.sleep(0.6)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "x"}), timeout=60) as r:
            return json.loads(r.read())
    except Exception as e:
        print("ERR", url, e); return None
os.makedirs("out/props", exist_ok=True)
S = [("basketball","nba","20221115",["40","45","47"]), ("basketball","nba","20231215",["58","40"]),
     ("basketball","nba","20250315",["58","59"]), ("basketball","nba","20260115",["100"]),
     ("basketball","nba","20260522",["100"]), ("basketball","nba","20261021",["100"]),
     ("football","nfl","20241020",["58","59"]), ("football","nfl","20251019",["58","59"]),
     ("football","nfl","20261025",["100"])]
for sp, lg, dt, provs in S:
    d = get(f"{SITE}/{sp}/{lg}/scoreboard?dates={dt}")
    for e in (d or {}).get("events", [])[:1]:
        eid = e["id"]
        for p in provs:
            pb = get(f"{C}/{sp}/leagues/{lg}/events/{eid}/competitions/{eid}/odds/{p}/propBets?lang=en&region=us&limit=1000")
            if pb:
                for x in pb.get("items", []):
                    x.pop("competition", None); x.pop("provider", None)
                json.dump(pb, open(f"out/props/{lg}_{dt}_{eid}_{p}.json", "w"))
                print(lg, dt, eid, p, pb.get("count"), pb.get("pageCount"))
        # the game's box score, to check which side won
        s = get(f"{SITE}/{sp}/{lg}/summary?event={eid}")
        if s: json.dump(s.get("boxscore", {}), open(f"out/props/{lg}_{dt}_{eid}_box.json", "w"))
