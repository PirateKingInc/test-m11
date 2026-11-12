"""Threes availability (read-only; never prints a key): (1) the poller's stored DK/FD
made-threes quotes from SharpAPI vs rebounds/assists (counts, players per game, first seen vs
tip); (2) The Odds API player_threes on two upcoming NBA events (DK, FD), credits from headers;
(3) fg3m in the live box scores."""
import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import pandas as pd
from sqlalchemy import create_engine

from betdata.config import get_settings
from betdata.db.engine import make_engine

out = {}
p = create_engine("sqlite:///file:data/poller.sqlite?mode=ro&uri=true")
q = pd.read_sql("select q.game_id, q.source, q.book, q.market, q.player_name, q.outcome, q.line,"
                " q.polled_at from quotes q where q.sport='nba' and q.player_name is not null "
                "and q.market in ('threes','assists','rebounds','points')", p)
q["polled_at"] = pd.to_datetime(q.polled_at, utc=True, format="mixed")
main = make_engine(get_settings().db_url)
g = pd.read_sql("select game_id, start_time_utc from games", main)
g["tip"] = pd.to_datetime(g.start_time_utc, utc=True, format="mixed")
q = q.merge(g[["game_id", "tip"]], on="game_id", how="left")
since = datetime.now(UTC) - timedelta(days=14)
q = q[q.polled_at >= since]
for mk, x in q.groupby("market"):
    first = x.groupby(["game_id", "book", "player_name"]).agg(first=("polled_at", "min"),
                                                             tip=("tip", "first"))
    lead = ((first.tip - first["first"]).dt.total_seconds() / 3600).dropna()
    out[mk] = {"rows_14d": int(len(x)), "sources": x.source.value_counts().to_dict(),
               "books": x.book.value_counts().to_dict(), "games": int(x.game_id.nunique()),
               "players_per_game_by_book": {b: round(float(y.groupby("game_id").player_name
                                                           .nunique().mean()), 1)
                                            for b, y in x.groupby("book")},
               "first_seen_hours_before_tip_median": round(float(lead.median()), 1)
               if len(lead) else None,
               "lines": x.line.value_counts().head(6).to_dict()}
key = os.environ.get("ODDS_API_KEY")
oa = {"key_present": bool(key)}
if key:
    c = httpx.Client(timeout=30)
    base = "https://api.the-odds-api.com/v4/sports/basketball_nba"
    r = c.get(f"{base}/events", params={"apiKey": key})
    oa["events_status"] = r.status_code
    oa["events_headers"] = {h: r.headers.get(h) for h in ("x-requests-used",
                                                          "x-requests-remaining",
                                                          "x-requests-last")}
    ev = r.json() if r.status_code == 200 else []
    now = datetime.now(UTC)
    ev = [e for e in ev if datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
          > now][:2]
    oa["sampled"] = []
    for e in ev:
        r = c.get(f"{base}/events/{e['id']}/odds",
                  params={"apiKey": key, "regions": "us", "markets": "player_threes",
                          "bookmakers": "draftkings,fanduel", "oddsFormat": "american"})
        rec = {"game": f"{e['away_team']} @ {e['home_team']}", "start": e["commence_time"],
               "status": r.status_code,
               "headers": {h: r.headers.get(h) for h in ("x-requests-used",
                                                         "x-requests-remaining",
                                                         "x-requests-last")}}
        if r.status_code == 200:
            for b in r.json().get("bookmakers", []):
                for m in b.get("markets", []):
                    rec.setdefault("books", {})[b["key"]] = {
                        "market": m["key"],
                        "players": len({o.get("description") for o in m["outcomes"]}),
                        "lines": sorted({o.get("point") for o in m["outcomes"]
                                         if o.get("point") is not None})[:6]}
        else:
            rec["error"] = r.text[:200]
        oa["sampled"].append(rec)
out["the_odds_api"] = oa
with main.connect() as m:
    out["box_fg3m"] = dict(m.exec_driver_sql(
        "select count(*), sum(fg3m is not null), max(known_at) from nba_player_games "
        "where known_at >= date('now','-14 day')").first()._mapping)
print(json.dumps(out, indent=1, default=str))
