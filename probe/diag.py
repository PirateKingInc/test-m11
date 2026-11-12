"""Action card hindsight replay (read-only): every OG card day Oct 31 - Nov 12 (Nov 5
excluded), at its stored selection time, Action selection with that day's actual official OG
card excluded (real poller + main DBs). Report only."""
import json
from datetime import datetime

from sqlalchemy import create_engine

from betdata import action as AC
from betdata.config import get_settings
from betdata.db.engine import make_engine
from betdata.ledger import ledger_dir, pick_id, read_rows

pdb = create_engine("sqlite:///file:data/poller.sqlite?mode=ro&uri=true")
main = make_engine(get_settings().db_url)
out = ledger_dir()
res = {r["pick_id"]: r for r in read_rows(out / "results.csv")}
picks = {r["pick_id"]: r for r in read_rows(out / "picks.csv")}
og_by_day: dict = {}
for r in read_rows(out / "card.csv"):
    if r["sport"] == "nba" and r["card"] == "yes" and r["card_date"] <= "2026-11-04":
        og_by_day.setdefault(r["card_date"], []).append(picks[r["pick_id"]]["dedup_key"])
for r in read_rows(out / "shadow_card.csv"):
    if r["sport"] == "nba" and r["shadow"] == "yes" and r["card_date"] >= "2026-11-06":
        og_by_day.setdefault(r["card_date"], []).append(picks[r["pick_id"]]["dedup_key"])
days = {}
for r in read_rows(out / "card_days.csv"):
    d = r["card_date"]
    if r["sport"] != "nba" or not ("2026-10-31" <= d <= "2026-11-12") or d == "2026-11-05":
        continue
    if (d <= "2026-11-04" and r["kind"] == "v1") or (d >= "2026-11-06" and r["kind"] == "shadow"):
        days[d] = r["selected_at"]
rows = {}
with pdb.connect() as p, main.connect() as m:
    for d, t in sorted(days.items()):
        og = {"picks": [{"dedup_key": k} for k in og_by_day.get(d, [])]}
        c = AC.choose(p, datetime.fromisoformat(d).date(), datetime.fromisoformat(t), og, m)
        for x in c["picks"]:
            pid = pick_id(x["dedup_key"])
            x["pick_id"] = pid
            x["result"] = res.get(pid, {}).get("result", "")
            x["profit"] = res.get(pid, {}).get("profit", "")
        rows[d] = {"selected_at": t, "og_picks": len(og["picks"]), "candidates": c["candidates"],
                   "reasons": c["reasons"], "picks": c["picks"]}
print(json.dumps(rows, indent=1, default=str))
