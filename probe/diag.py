"""Live check (read-only): F threes picks, threes quotes since #400, last poller runs."""
from datetime import datetime, timezone

from sqlalchemy import create_engine, func, select

from betdata.alerts.db import alerts, poller_runs, quotes

T = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
pdb = create_engine("sqlite:///file:data/poller.sqlite?mode=ro&uri=true")
with pdb.connect() as p:
    rows = p.execute(select(alerts).where(alerts.c.signal == "snipe", alerts.c.market == "threes")
                     .order_by(alerts.c.created_at)).fetchall()
    print("F THREES PICKS:", len(rows))
    for r in rows[:10]:
        print(r.created_at, r.game_label, r.player_name, r.side, r.line, r.price, r.book,
              r.fair_prob, r.book_prob, r.tip)
    print("SNIPE BY MARKET since 12 UTC:", p.execute(select(alerts.c.market, func.count()).where(
        alerts.c.signal == "snipe", alerts.c.created_at >= T).group_by(alerts.c.market)).all())
    print("QUOTES BY MARKET/BOOK since 12 UTC:", p.execute(
        select(quotes.c.market, quotes.c.book, func.count(), func.max(quotes.c.polled_at))
        .where(quotes.c.polled_at >= T, quotes.c.sport == "nba",
               quotes.c.market.in_(("threes", "assists", "rebounds")))
        .group_by(quotes.c.market, quotes.c.book)).all())
    for r in p.execute(select(poller_runs).order_by(poller_runs.c.run_id.desc()).limit(5)):
        print("RUN", r.run_id, r.started_at, r.finished_at, r.status, r.cycles,
              (r.message or "")[:300].replace("\n", " | "))
print("LAST RUNS: done")
