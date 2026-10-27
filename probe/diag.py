"""Replay every live NFL qualifying change through the new M12 value test (REVIEW M17-0)."""

import json
import sqlite3
from datetime import datetime

from sqlalchemy import select

from betdata.alerts import nfl as N
from betdata.alerts.rules import qualifies_nfl
from betdata.config import get_settings
from betdata.db.engine import make_engine
from betdata.db.schema import games

eng = make_engine(get_settings().db_url)
p = sqlite3.connect("data/poller.sqlite")
p.row_factory = sqlite3.Row
rows = p.execute("select * from status_events where sport='nfl' and qualifies=1 "
                 "order by detected_at").fetchall()
print("qualifying NFL events:", len(rows), "cols:", rows[0].keys() if rows else None)
M = None
kept = 0
for r in rows:
    d = json.loads(r["details"] or "{}")
    now = datetime.fromisoformat(str(r["detected_at"]).replace(" ", "T"))
    if now.tzinfo is None:
        from datetime import UTC
        now = now.replace(tzinfo=UTC)
    if M is None:
        M = N.Models(eng, now)
    cur = json.loads(p.execute("select value from poller_state where key=?",
                               (f"nfl:status:{r['game_id']}",)).fetchone()[0])
    with eng.connect() as c:
        g = c.execute(select(games).where(games.c.game_id == r["game_id"])).one()
        known = N.m12_known(c, g)
    after = {k: dict(v) for k, v in cur.items()}
    if r["player_key"] in after:
        after[r["player_key"]]["status"] = r["new_status"]
    before = {k: dict(v) for k, v in after.items()}
    if r["old_status"] is None:
        before.pop(r["player_key"], None)
    elif r["player_key"] in before:
        before[r["player_key"]]["status"] = r["old_status"]
    try:
        pb = M.play_probs(g, N.observations(before, known), now)
        pa = M.play_probs(g, N.observations(after, known), now)
    except Exception as e:  # noqa: BLE001
        print(r["player_name"], "ERROR", repr(e))
        continue
    gs = d.get("gsis")
    b, a = pb.get(gs), pa.get(gs)
    q = (a is not None and qualifies_nfl(r["position"], r["weight"], b if b is not None
         else r["prob_before"], a, starter=r["position"] in N.STARTER_GROUPS))
    kept += bool(q) if a is not None else 1
    print(f"{r["detected_at"]} g{r['game_id']} {r['player_name']} ({r['position']}) "
          f"{r['old_status']}->{r['new_status']} espn {r['prob_before']}->{r['prob_after']} "
          f"M12 {b}->{a} qualifies_now={q if a is not None else 'unchanged'}")
print("still qualifying:", kept, "of", len(rows))
