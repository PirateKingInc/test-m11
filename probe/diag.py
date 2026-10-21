"""M15 source verification (throwaway, runs on an Actions runner)."""
import io, json, os, time, urllib.request, urllib.error
import pandas as pd

TOK = os.environ.get("GH_TOKEN", "")
def get(url, gh=False, raw=False):
    h = {"User-Agent": "betdata-probe"}
    if gh and TOK:
        h["Authorization"] = f"Bearer {TOK}"
    time.sleep(1.0)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
            b = r.read()
            return r.status, (b if raw else json.loads(b))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return str(e)[:80], None

def assets(repo, tag):
    out, page = [], 1
    while True:
        st, d = get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", gh=True) if page == 1 else (None, None)
        if not d: break
        rel_id = d["id"]
        while True:
            st2, a = get(f"https://api.github.com/repos/{repo}/releases/{rel_id}/assets?per_page=100&page={page}", gh=True)
            if not a: break
            out += [(x["name"], x["size"], x["updated_at"][:10]) for x in a]
            page += 1
        break
    return out

def show(repo, tag, filt=None):
    a = assets(repo, tag)
    a = [x for x in a if not filt or filt in x[0]]
    print(f"\n## {repo} {tag}: {len(a)} assets")
    for x in sorted(a)[:200]:
        print("  ", x)

def cols(url, n=3):
    st, b = get(url, raw=True)
    if not b:
        print("  !", url, st); return None
    df = pd.read_parquet(io.BytesIO(b)) if url.endswith("parquet") else pd.read_csv(io.BytesIO(b), low_memory=False)
    print(f"  {url.split('/')[-1]}: {df.shape}; cols={list(df.columns)}")
    print(df.head(n).to_string()[:3000])
    return df

NV = "https://github.com/nflverse/nflverse-data/releases/download"
for tag, f in [("pbp_participation", "csv"), ("ftn_charting", "csv"), ("nextgen_stats", "csv"),
               ("pfr_advstats", "csv"), ("depth_charts", "csv"), ("stats_player", "csv"),
               ("player_stats", "csv"), ("officials", "csv"), ("snap_counts", "csv")]:
    show("nflverse/nflverse-data", tag, f)
for u in [f"{NV}/pbp_participation/pbp_participation_2023.parquet",
          f"{NV}/pbp_participation/pbp_participation_2024.parquet",
          f"{NV}/pbp_participation/pbp_participation_2025.parquet",
          f"{NV}/ftn_charting/ftn_charting_2024.parquet",
          f"{NV}/depth_charts/depth_charts_2024.parquet",
          f"{NV}/depth_charts/depth_charts_2025.parquet",
          f"{NV}/depth_charts/depth_charts_2026.parquet",
          f"{NV}/nextgen_stats/ngs_receiving.parquet",
          f"{NV}/pfr_advstats/advstats_week_pass_2024.parquet",
          f"{NV}/pfr_advstats/advstats_week_def_2024.parquet",
          f"{NV}/stats_player/stats_player_week_2025.parquet"]:
    cols(u, 2)

print("\n# sportsdataverse releases")
st, rels = get("https://api.github.com/repos/sportsdataverse/sportsdataverse-data/releases?per_page=100", gh=True)
print([r["tag_name"] for r in rels or []])
show("sportsdataverse/sportsdataverse-data", "espn_nba_pbp")
SD = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
pbp = cols(f"{SD}/espn_nba_pbp/play_by_play_2024.parquet", 5)
if pbp is not None:
    print(pbp["type_text"].value_counts().head(40).to_string() if "type_text" in pbp else "")
    sub = pbp[pbp.get("type_text", pd.Series(dtype=str)).astype(str).str.contains("Substitution", na=False)]
    print(sub.head(5).T.to_string()[:4000])

print("\n# shufinskiy/nba_data")
st, rels = get("https://api.github.com/repos/shufinskiy/nba_data/releases?per_page=20", gh=True)
print(st, [(r["tag_name"], r["published_at"]) for r in rels or []][:10])
st, d = get("https://api.github.com/repos/shufinskiy/nba_data", gh=True)
print(st, d and (d.get("license"), d.get("pushed_at"), d.get("description")))
st, tree = get("https://api.github.com/repos/shufinskiy/nba_data/git/trees/main?recursive=1", gh=True)
print([t["path"] for t in (tree or {}).get("tree", [])][:120])
for lic in ["nflverse/nflverse-data", "sportsdataverse/sportsdataverse-data", "sportsdataverse/hoopR"]:
    st, d = get(f"https://api.github.com/repos/{lic}", gh=True)
    print(lic, st, d and d.get("license"))

print("\n# ESPN prop bets")
C = "https://sports.core.api.espn.com/v2/sports"
def props(sport, league, eid):
    st, d = get(f"{C}/{sport}/leagues/{league}/events/{eid}/competitions/{eid}/odds?lang=en&region=us")
    provs = [(i.get("provider", {}).get("id"), i.get("provider", {}).get("name")) for i in (d or {}).get("items", [])]
    print(f"  {league} {eid} odds {st} providers={provs}")
    for pid in [p for p, _ in provs if p] or ["58", "100", "1002"]:
        st, pb = get(f"{C}/{sport}/leagues/{league}/events/{eid}/competitions/{eid}/odds/{pid}/propBets?lang=en&region=us&limit=1000")
        n = (pb or {}).get("count")
        print(f"    provider {pid}: {st} count={n}")
        if pb and pb.get("items"):
            it = pb["items"][:2]
            print("     ", json.dumps(it)[:1500])
            types = {}
            for x in pb["items"]:
                t = (x.get("type") or {}).get("name")
                types[t] = types.get(t, 0) + 1
            print("      types", types)
SITE = "https://site.api.espn.com/apis/site/v2/sports"
def ids(sport, league, date):
    st, d = get(f"{SITE}/{sport}/{league}/scoreboard?dates={date}")
    return [e["id"] for e in (d or {}).get("events", [])]
for date in ["20261021", "20261022", "20260301", "20250301", "20240301", "20230301", "20220301", "20210301", "20200301", "20190301"]:
    i = ids("basketball", "nba", date)
    print("nba", date, i[:3])
    if i: props("basketball", "nba", i[0])
for date in ["20261025", "20261018", "20251019", "20241020", "20231022", "20221023", "20211024", "20201025"]:
    i = ids("football", "nfl", date)
    print("nfl", date, i[:3])
    if i: props("football", "nfl", i[0])
