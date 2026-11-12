"""Hindsight replays (read-only, real poller + main DBs), Oct 31 - Nov 12 (Nov 5 excluded):
(1) the Action card with live adds: the morning Action card at the stored OG selection time, then
live adds evaluated at every real sweep (each distinct pick created_at after it), 6 AM-11 PM ET;
(2) the OG + threes shadow at the stored OG selection time. Report only."""
import json
from datetime import datetime

from sqlalchemy import create_engine, select

from betdata import action as AC
from betdata import og_ranking as OR
from betdata.alerts.card import V1_SIGNALS, day_picks
from betdata.alerts.db import state
from betdata.alerts.grade import utc
from betdata.alerts.rules import EASTERN
from betdata.config import get_settings
from betdata.db.engine import make_engine
from betdata.ledger import ledger_dir, pick_id, read_rows

pdb = create_engine("sqlite:///file:data/poller.sqlite?mode=ro&uri=true")
main = make_engine(get_settings().db_url)
out = ledger_dir()
res = {r["pick_id"]: r for r in read_rows(out / "results.csv")}


def graded(x):
    pid = pick_id(x["dedup_key"])
    return {**x, "pick_id": pid, "result": res.get(pid, {}).get("result", ""),
            "profit": res.get(pid, {}).get("profit", "")}


rows = {}
with pdb.connect() as p, main.connect() as m:
    st = {r.key: r.value for r in p.execute(select(state))}
    for d in [f"2026-10-{x}" for x in (31,)] + [f"2026-11-{x:02d}" for x in range(1, 13)]:
        if d == "2026-11-05":
            continue
        og = st.get(f"card:nba:{d}" if d <= "2026-11-04" else f"shadow:nba:{d}") or {}
        if not og.get("selected_at"):
            continue
        day = datetime.fromisoformat(d).date()
        t = datetime.fromisoformat(og["selected_at"])
        ac = AC.choose(p, day, t, og, m)
        ac["selected_at"] = t.isoformat()
        sweeps = sorted({utc(a.created_at) for a in day_picks(p, "nba", day, V1_SIGNALS)
                         if utc(a.created_at) > t})
        s, logs = {"adds": []}, []
        for now in sweeps:
            if not AC.ADD_HOURS[0] <= now.astimezone(EASTERN).hour < AC.ADD_HOURS[1]:
                continue
            new, log = AC.evaluate_adds(p, day, now, og, ac, s, m)
            s["adds"] = s["adds"] + new
            s["evaluated_through"] = now.isoformat()
            logs.append(log)
        sh = OR.threes_shadow(p, "nba", day, t, m, og=og)
        threes_pool = sum(1 for a in day_picks(p, "nba", day, V1_SIGNALS) if a.market == "threes")
        rows[d] = {"selected_at": t.isoformat(), "og": [x["dedup_key"] for x in og.get("picks", [])],
                   "morning": [graded(x) for x in ac["picks"]],
                   "adds": [graded(x) for x in s["adds"]], "sweeps": len(logs),
                   "why_not": {k: sum(lg["why_not"].get(k, 0) for lg in logs)
                               for k in {k for lg in logs for k in lg["why_not"]}},
                   "shadow": [graded(x) for x in sh["picks"]], "threes_picks_that_day": threes_pool,
                   "og_v2": [x["dedup_key"] for x in OR.card(p, "nba", day, t, m)["picks"]]}
print(json.dumps(rows, indent=1, default=str))
