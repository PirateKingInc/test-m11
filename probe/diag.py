"""Dry run of the full morning selection on COPIES of today's real data (the real state is never
written): today's stored cards removed from the copy, Ranking v2 dated to today for this test,
the real morning path (Next Gen, Spread, OG Ranking v2 + Top 3, the parlay), then the NBA push
text built but NOT sent."""
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete

from betdata import morning as M
from betdata import og_ranking as OR
from betdata import spread as SP
from betdata.alerts import card as CD
from betdata.alerts.db import engine_for, get_state, state
from betdata.alerts.poller import Keys, Poller
from betdata.alerts.rules import EASTERN
from betdata.db.engine import make_engine
from betdata.ledger import ledger_dir

tmp = Path(tempfile.mkdtemp())
shutil.copy("data/poller.sqlite", tmp / "poller.sqlite")
shutil.copy("data/betdata.sqlite", tmp / "betdata.sqlite")
shutil.copytree(ledger_dir(), tmp / "ledger")
now = datetime.now(UTC)
day = now.astimezone(EASTERN).date()
d = day.isoformat()
pdb = engine_for(tmp / "poller.sqlite")
main = make_engine(f"sqlite:///{tmp / 'betdata.sqlite'}")
with pdb.begin() as p:
    for k in (f"card:nba:{d}", f"shadow:nba:{d}", f"spread:nba:{d}", f"og3:nba:{d}",
              f"parlay:nba:{d}", f"morning:{d}"):
        p.execute(delete(state).where(state.c.key == k))
OR.start = lambda: day  # Ranking v2 from today, for this dry run only
CD.in_ledger = lambda *a: False
CD.shadow_in_ledger = lambda *a: False
SP.in_ledger = lambda *a, **k: False
M.grade = lambda *a: None  # today's picks already exist and are graded
M.engine = lambda *a: 0
poller = Poller(main, pdb, Keys(os.environ.get("SHARPAPI_KEY") or None, None, None))
rep = M.run(main, pdb, poller, now, out=tmp / "ledger", build=None)
print("REPORT", json.dumps({k: getattr(rep, k) for k in ("sports", "steps", "cards", "shadow",
                                                          "spread", "og3", "parlay", "errors")}))
with pdb.connect() as p:
    for k in ("card", "shadow", "spread", "og3"):
        c = get_state(p, f"{k}:nba:{d}") or {}
        print(k.upper(), json.dumps([{kk: x.get(kk) for kk in ("dedup_key", "label", "stake",
                                                              "fair_prob", "price", "median",
                                                              "line", "ev")}
                                     for x in c.get("picks", [])]))
        if k == "shadow":
            print("OG_REJECTED", json.dumps(c.get("og_rejected", {})))
            print("OG_FILTERED", len(c.get("filtered", {})))
            print("OG_RANKING", c.get("ranking"), "NOTE", CD.card_note(c))
    from betdata import official as O

    title, body = M.message(p, O.dataset(tmp / "ledger"), "nba", now)
print("PUSH TITLE:", title)
print(body)
print("TOP3 IN PUSH:", "og3" in body or "Top 3" in body)
