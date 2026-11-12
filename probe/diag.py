"""Threes-phase equivalence proof (read-only): every stored NBA card day since Nov 5, replayed at
its stored selection time with the pre-phase alerts/card.py (3dc3eab, before) and this stack's
(after): Next Gen, official OG and the Top 3 shadow; and each against the stored card."""
import importlib.util
import json
from datetime import datetime

from sqlalchemy import create_engine, select

from betdata import og_ranking as OR
from betdata.alerts import card as after
from betdata.alerts.db import state
from betdata.config import get_settings
from betdata.db.engine import make_engine

spec = importlib.util.spec_from_file_location("card_before", "probe/card_before.py")
before = importlib.util.module_from_spec(spec)
spec.loader.exec_module(before)

pdb = create_engine("sqlite:///file:data/poller.sqlite?mode=ro&uri=true")
main = make_engine(get_settings().db_url)


def sig(c):
    return [(x["dedup_key"], x["label"], x["stake"]) for x in (c or {}).get("picks", [])]


out = {"nextgen": [], "og": [], "top3": []}
with pdb.connect() as p, main.connect() as m:
    st = {r.key: r.value for r in p.execute(select(state))}
    for key, c in st.items():
        if not key.startswith(("card:nba:", "shadow:nba:")):
            continue
        c = c or {}
        if not c.get("day") or c["day"] < "2026-11-05" or not c.get("selected_at"):
            continue
        day = datetime.fromisoformat(c["day"]).date()
        t = datetime.fromisoformat(c["selected_at"])
        ft = after.first_tip(m, "nba", day)
        if key.startswith("card:"):
            b = before.decide(p, m, "nba", day, t, ft, None)
            a = after.decide(p, m, "nba", day, t, ft, None)
            kind = "nextgen"
        else:
            b = before.decide_og(p, m, "nba", day, t, ft)
            a = after.decide_og(p, m, "nba", day, t, ft)
            kind = "og"
            s3 = st.get(OR.top3_key("nba", c["day"]))
            if s3 is not None:
                a3 = OR.top3_of(a)
                out["top3"].append({"day": c["day"], "same_as_stored": sig(a3) == sig(s3),
                                    "n": len(sig(a3))})
        out[kind].append({"day": c["day"], "selected_at": c["selected_at"], "n": len(sig(a)),
                          "identical": a == b, "same_picks_order_labels_stakes": sig(a) == sig(b),
                          "equals_stored": sig(a) == sig(c), "picks": sig(a)})
for k in out:
    out[k].sort(key=lambda r: r["day"])
print(json.dumps(out, indent=1, default=str))
for k in ("nextgen", "og"):
    print(k.upper(), "ALL IDENTICAL:", all(r["identical"] for r in out[k]), len(out[k]),
          "ALL EQUAL STORED:", all(r["equals_stored"] for r in out[k]))
print("TOP3 ALL EQUAL STORED:", all(r["same_as_stored"] for r in out["top3"]), len(out["top3"]))
