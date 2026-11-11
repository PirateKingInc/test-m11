"""OG Ranking v2 hindsight replay (read-only): each OG card day Oct 31 - Nov 10 (Nov 5 excluded,
as in the OG record), at the day's stored selection time, today's rules (Rules v2 on every day),
only picks created before the selection: the actual EV card vs Ranking v2 top 5 / top 3. Two
pools: Live at selection (the live rule) and every pick created before selection."""
import json
from collections import defaultdict
from datetime import datetime

from sqlalchemy import create_engine

from betdata import eligibility as EL
from betdata import og_ranking as OR
from betdata.config import get_settings
from betdata.db.engine import make_engine
from betdata.ledger import ledger_dir, pick_id, read_rows

EL.applies = lambda tip: tip is not None  # today's rules on every day
pdb = create_engine("sqlite:///file:data/poller.sqlite?mode=ro&uri=true")
main = make_engine(get_settings().db_url)
out = ledger_dir()
res = {r["pick_id"]: r for r in read_rows(out / "results.csv")}
days = {}
for r in read_rows(out / "card_days.csv"):
    if r["sport"] != "nba" or not ("2026-10-31" <= r["card_date"] <= "2026-11-10"):
        continue
    if r["card_date"] == "2026-11-05":
        continue
    if (r["card_date"] <= "2026-11-04" and r["kind"] == "v1") or (
            r["card_date"] >= "2026-11-06" and r["kind"] == "shadow"):
        days[r["card_date"]] = r["selected_at"]

import betdata.alerts.labels as LB  # noqa: E402

real_available = LB.available
rows = {}
for live in (True, False):
    LB.available = real_available if live else (lambda p, a, now: True)
    with pdb.connect() as p, main.connect() as m:
        for d, t in sorted(days.items()):
            day = datetime.fromisoformat(d).date()
            ranked, filtered, rejected, n = OR.ranked_pool(p, "nba", day,
                                                          datetime.fromisoformat(t), m)
            picks = [{"pid": pick_id(a.dedup_key), "player": a.player_name,
                      "market": a.market, "side": a.side, "line": a.line, "price": a.price,
                      "fair": round(a.fair_prob, 3), "median": OR.median_of(a),
                      "result": res.get(pick_id(a.dedup_key), {}).get("result", ""),
                      "profit": res.get(pick_id(a.dedup_key), {}).get("profit", "")}
                     for a in ranked[:5]]  # fmt: skip
            rows.setdefault("live" if live else "created", {})[d] = {
                "selected_at": t, "eligible": n, "filtered": len(filtered),
                "rejected": dict(sorted(defaultdict(int, {}).items())) or
                {k: list(rejected.values()).count(k) for k in set(rejected.values())},
                "picks": picks}
print(json.dumps(rows, indent=1, default=str))
