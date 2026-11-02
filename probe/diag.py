"""M19 review dry run on the live databases, against a COPY of the ledger (out/ledger): the
official picks' review rows and the tag counts. Nothing is written to the repo's ledger."""
import csv
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine

from betdata import official as O
from betdata import review as RV
from betdata.config import get_settings
from betdata.db.engine import make_engine

out = Path("out/ledger")
shutil.copytree("ledger", out)
peng = create_engine("sqlite:///file:data/poller.sqlite?mode=ro&uri=true", future=True)
t = time.time()
with peng.connect() as p, make_engine(get_settings().db_url).connect() as m:
    print("sync", RV.sync(p, m, out, datetime.now(UTC)), f"{time.time() - t:.0f}s")
rp = {r["pick_id"]: r for r in csv.DictReader((out / "review_pick.csv").open())}
rr = {r["pick_id"]: r for r in csv.DictReader((out / "review_result.csv").open())}
rows = O.dataset(out)
print("sources", {s: sum(1 for r in rp.values() if r["source"] == s)
                  for s in ("alert", "as-of rebuild")})
print("teams blank", sum(1 for r in rp.values() if not r["team"]), "of", len(rp))
for r in O.official(rows):
    a, b = rp.get(r["pick_id"], {}), rr.get(r["pick_id"], {})
    print("OFF", r["day"], r["game"], r["player"], r["market"], r["side"], r["line"], r["result"],
          "|", a.get("team"), "vs", a.get("opponent"), "med", a.get("proj_median"), "min",
          a.get("proj_minutes"), a.get("source"), "| act", b.get("actual_stat"), "min",
          b.get("actual_minutes"), "clv", b.get("clv"), "tag", b.get("tag"), "chg",
          b.get("status_changes"), "st", (a.get("statuses") or "")[:150])
print("tags all", O.tag_counts(rows))
print("tags official", O.tag_counts(O.official(rows)))
