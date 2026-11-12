"""OG Ranking v2 equivalence proof (read-only): every stored NBA card day since Nov 5, replayed
at its stored selection time with main's alerts/card.py (before) and this branch's (after)."""
import importlib.util
import json
from datetime import datetime

from sqlalchemy import create_engine, select

from betdata.alerts import card as after
from betdata.alerts.db import state
from betdata.alerts.rules import EASTERN
from betdata.config import get_settings
from betdata.db.engine import make_engine

spec = importlib.util.spec_from_file_location("card_before", "probe/card_before.py")
before = importlib.util.module_from_spec(spec)
spec.loader.exec_module(before)

pdb = create_engine("sqlite:///file:data/poller.sqlite?mode=ro&uri=true")
main = make_engine(get_settings().db_url)


def sig(c):
    return [(x["dedup_key"], x["label"], x["stake"]) for x in (c or {}).get("picks", [])]


out = {"nextgen": [], "og": []}
with pdb.connect() as p, main.connect() as m:
    for st in p.execute(select(state).where(state.c.key.like("card:nba:%")
                                            | state.c.key.like("shadow:nba:%"))):
        c = st.value or {}
        if not c.get("day") or c["day"] < "2026-11-05" or not c.get("selected_at"):
            continue
        day = datetime.fromisoformat(c["day"]).date()
        t = datetime.fromisoformat(c["selected_at"])
        ft = after.first_tip(m, "nba", day)
        if st.key.startswith("card:"):
            b = before.decide(p, m, "nba", day, t, ft, None)
            a = after.decide(p, m, "nba", day, t, ft, None)
            kind = "nextgen"
        else:
            b = before.decide_og(p, m, "nba", day, t, ft)
            a = after.decide_og(p, m, "nba", day, t, ft)
            kind = "og"
        out[kind].append({"day": c["day"], "selected_at": c["selected_at"],
                          "n": len(sig(a)), "identical": a == b,
                          "same_picks_order_labels_stakes": sig(a) == sig(b),
                          "equals_stored": sig(a) == sig(c),
                          "picks": sig(a), "stored": sig(c)})
for k in out:
    out[k].sort(key=lambda r: r["day"])
print(json.dumps(out, indent=1, default=str))
print("NEXTGEN ALL IDENTICAL:", all(r["identical"] for r in out["nextgen"]), len(out["nextgen"]))
print("OG ALL IDENTICAL:", all(r["identical"] for r in out["og"]), len(out["og"]))
