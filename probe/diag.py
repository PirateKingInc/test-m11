"""Nov 13 verification (read-only): today's stored NBA cards, the live-adds log, F threes."""
import json

from sqlalchemy import create_engine, select

from betdata.alerts.db import alerts, state

pdb = create_engine("sqlite:///file:data/poller.sqlite?mode=ro&uri=true")
D = "2026-11-13"
F = ("dedup_key", "label", "stake", "fair_prob", "fair_prob_cal", "price", "median", "line",
     "market", "side", "player", "book", "reason", "reasons", "posted", "og_quality", "note")
with pdb.connect() as p:
    st = {r.key: r.value for r in p.execute(select(state).where(state.c.key.like(f"%{D}%")))}
    print("KEYS", sorted(st))
    for k in (f"card:nba:{D}", f"shadow:nba:{D}", f"og3:nba:{D}", f"action:nba:{D}",
              f"actionadds:nba:{D}", f"ogthrees:nba:{D}", f"spread:nba:{D}", f"morning:{D}"):
        c = st.get(k)
        if c is None:
            print(k, "MISSING")
            continue
        if not isinstance(c, dict):
            print(k, json.dumps(c, default=str)[:1500])
            continue
        print(k, "selected_at", c.get("selected_at"), "ranking", c.get("ranking"),
              "source", c.get("source"), "no_lines", c.get("no_lines"))
        for x in c.get("picks", []) or c.get("adds", []):
            print("   ", json.dumps({f: x.get(f) for f in F if f in x}, default=str))
        if k.startswith("shadow:"):
            from collections import Counter
            print("   FILTERED", dict(Counter((c.get("filtered") or {}).values())))
            print("   REJECTED", dict(Counter((c.get("og_rejected") or {}).values())))
        if k.startswith("actionadds:"):
            print("   EVALUATED_THROUGH", c.get("evaluated_through"))
            for lg in c.get("log", [])[-12:]:
                print("   LOG", json.dumps(lg, default=str))
        if k.startswith("morning:"):
            print("   ", json.dumps({kk: v for kk, v in c.items() if kk != "body"},
                                    default=str)[:1500])
    rows = p.execute(select(alerts).where(alerts.c.signal == "snipe", alerts.c.market == "threes")
                     .order_by(alerts.c.created_at)).fetchall()
    print("F THREES PICKS:", len(rows))
    for r in rows[:12]:
        print("   ", r.created_at, r.game_label, r.player_name, r.side, r.line, r.price, r.book,
              round(r.fair_prob, 4), round(r.book_prob, 4), r.tip, r.sent, r.send_status)
print("DONE")
