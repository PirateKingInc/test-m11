"""Rules v3 proof (read-only on the real data; the dry run writes only to COPIES):
(1) every stored NBA card day since Nov 5 replayed with this branch: Next Gen and official OG
    equal the stored cards (Rules v3 applies from Nov 13 only); Top 3 equals stored;
(2) the Nov 13 morning selection dry run at 6:15 AM ET on a copy of today's data, push built
    but NOT sent."""
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select

from betdata import morning as M
from betdata import og_ranking as OR
from betdata import spread as SP
from betdata.alerts import card as CD
from betdata.alerts.db import engine_for, get_state, state
from betdata.alerts.poller import Keys, Poller
from betdata.config import get_settings
from betdata.db.engine import make_engine
from betdata.ledger import ledger_dir


def sig(c):
    return [(x["dedup_key"], x["label"], x["stake"]) for x in (c or {}).get("picks", [])]


pdb = create_engine("sqlite:///file:data/poller.sqlite?mode=ro&uri=true")
main = make_engine(get_settings().db_url)
eq = {"nextgen": [], "og": [], "top3": []}
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
        ft = CD.first_tip(m, "nba", day)
        if key.startswith("card:"):
            a = CD.decide(p, m, "nba", day, t, ft, None)
            eq["nextgen"].append((c["day"], sig(a) == sig(c), len(sig(a))))
        else:
            a = CD.decide_og(p, m, "nba", day, t, ft)
            eq["og"].append((c["day"], sig(a) == sig(c), len(sig(a))))
            s3 = st.get(OR.top3_key("nba", c["day"]))
            if s3 is not None:
                eq["top3"].append((c["day"], sig(OR.top3_of(a)) == sig(s3)))
for k in eq:
    eq[k].sort()
    print(k.upper(), eq[k])
print("NEXTGEN ALL EQUAL STORED:", all(x[1] for x in eq["nextgen"]), len(eq["nextgen"]))
print("OG ALL EQUAL STORED:", all(x[1] for x in eq["og"]), len(eq["og"]))
print("TOP3 ALL EQUAL STORED:", all(x[1] for x in eq["top3"]), len(eq["top3"]))

# (2) the Nov 13 morning dry run on copies
tmp = Path(tempfile.mkdtemp())
shutil.copy("data/poller.sqlite", tmp / "poller.sqlite")
shutil.copy("data/betdata.sqlite", tmp / "betdata.sqlite")
shutil.copytree(ledger_dir(), tmp / "ledger")
now = datetime(2026, 11, 13, 11, 15, 30, tzinfo=UTC)  # 6:15 AM ET Nov 13
d = "2026-11-13"
pdb2 = engine_for(tmp / "poller.sqlite")
main2 = make_engine(f"sqlite:///{tmp / 'betdata.sqlite'}")
CD.in_ledger = lambda *a: False
CD.shadow_in_ledger = lambda *a: False
SP.in_ledger = lambda *a, **k: False
M.grade = lambda *a: None
M.engine = lambda *a: 0
sent = []
poller = Poller(main2, pdb2, Keys(os.environ.get("SHARPAPI_KEY") or None, None, None))
rep = M.run(main2, pdb2, poller, now, out=tmp / "ledger", build=None)
print("REPORT", json.dumps({k: getattr(rep, k, None) for k in (
    "sports", "steps", "cards", "shadow", "spread", "og3", "action", "ogthrees", "slate",
    "parlay", "errors")}, default=str))
with pdb2.connect() as p:
    for k in ("card", "shadow", "og3", "action", "ogthrees"):
        c = get_state(p, f"{k}:nba:{d}") or {}
        print(k.upper(), json.dumps([{kk: x.get(kk) for kk in (
            "dedup_key", "stake", "fair_prob", "price", "median", "line", "market", "side",
            "player", "reasons")} for x in c.get("picks", [])]))
        if k == "shadow":
            from collections import Counter

            print("OG_FILTERED", dict(Counter((c.get("filtered") or {}).values())))
            print("OG_REJECTED", dict(Counter((c.get("og_rejected") or {}).values())))
    from betdata import official as O

    title, body = M.message(p, O.dataset(tmp / "ledger"), "nba", now)
print("PUSH TITLE:", title)
print(body)
print("DONE")
