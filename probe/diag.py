"""Order proof (read-only): OG Ranking v2 on raw vs F-calibrated probability, on every backtest
day (Oct 31 - Nov 10, today's rules) and every stored OG card day from Nov 11 on, each at its
stored selection time; also vs the stored card where Ranking v2 chose it."""
import json
from datetime import date, datetime

from sqlalchemy import create_engine, select

from betdata import eligibility as EL
from betdata import f_calibration as FC
from betdata import og_ranking as OR
from betdata.alerts.db import state
from betdata.config import get_settings
from betdata.db.engine import make_engine
from betdata.ledger import ledger_dir, read_rows

EL.applies = lambda tip: tip is not None  # today's rules on every day (as the backtest)
pdb = create_engine("sqlite:///file:data/poller.sqlite?mode=ro&uri=true")
main = make_engine(get_settings().db_url)
out = ledger_dir()
fit = FC.current(out)
if fit is None:  # not fitted on the runner yet: the same fit from the same ledger
    ps, ys = FC.points(out)
    fit, _ = FC.fit_points(ps, ys, date.today())
print("FIT", fit)
days = {}
for r in read_rows(out / "card_days.csv"):
    if r["sport"] == "nba" and r["kind"] in ("v1", "shadow") and r["card_date"] != "2026-11-05" \
            and r["card_date"] >= "2026-10-31":
        days[r["card_date"]] = r["selected_at"]
rows, ok = [], True
with pdb.connect() as p, main.connect() as m:
    stored = {st.key.split(":")[-1]: st.value for st in p.execute(
        select(state).where(state.c.key.like("shadow:nba:%")))}
    for d, t in sorted(days.items()):
        day = datetime.fromisoformat(d).date()
        now = datetime.fromisoformat(t)
        raw, _, rej_r, n = OR.ranked_pool(p, "nba", day, now, m, None)
        cal, _, rej_c, _ = OR.ranked_pool(p, "nba", day, now, m, fit)
        same = [a.dedup_key for a in raw] == [a.dedup_key for a in cal] and rej_r == rej_c
        st = stored.get(d) or {}
        vs = None
        if st.get("ranking") == "v2":
            vs = [x["dedup_key"] for x in st["picks"]] == [a.dedup_key for a in cal][:5]
        ok &= same and vs is not False
        rows.append({"day": d, "eligible": n, "top5": [a.dedup_key for a in raw][:5],
                     "identical_raw_vs_calibrated": same, "equals_stored_v2_card": vs})
print(json.dumps(rows, indent=1))
print("ALL IDENTICAL:", ok, len(rows))
