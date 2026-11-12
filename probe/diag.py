"""Live check (read-only): F threes picks stored so far, poller runs since #400, and threes
openers/quotes counts."""
from sqlalchemy import create_engine, func, select

from betdata.alerts.db import alerts, poller_runs

pdb = create_engine("sqlite:///file:data/poller.sqlite?mode=ro&uri=true")
with pdb.connect() as p:
    rows = p.execute(select(alerts).where(alerts.c.signal == "snipe", alerts.c.market == "threes")
                     .order_by(alerts.c.created_at)).fetchall()
    print("F THREES PICKS:", len(rows))
    for r in rows[:10]:
        print(r.created_at, r.game_label, r.player_name, r.side, r.line, r.price, r.book,
              r.fair_prob, r.book_prob, r.tip)
    print("SNIPE BY MARKET:", p.execute(select(alerts.c.market, func.count()).where(
        alerts.c.signal == "snipe", alerts.c.created_at >= "2026-11-12").group_by(
        alerts.c.market)).fetchall())
    print("LAST RUNS:", [tuple(r) for r in p.execute(select(poller_runs).order_by(
        poller_runs.c.run_id.desc()).limit(4)).fetchall()])
