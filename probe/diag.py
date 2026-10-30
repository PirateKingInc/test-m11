"""Pick-labels probe: build the page from the live databases (out/real), then a PREVIEW in
this throwaway runner only: the label rules applied to the existing M18 picks as of each pick's
creation time (out/preview). Nothing is saved back: probe-env never writes the caches."""

import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

from betdata.alerts import labels as LB
from betdata.alerts.db import engine_for


def sh(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    print("$", " ".join(cmd), "rc", r.returncode, r.stdout[-600:], r.stderr[-1200:])


def ts(x):
    return None if x is None else datetime.fromisoformat(x).replace(tzinfo=UTC)


sh("uv", "run", "--no-dev", "betdata", "page", "build", "--site", "out/real")
eng = engine_for("data/poller.sqlite")
db = sqlite3.connect("data/poller.sqlite")
rows = db.execute("select alert_id, signal, sport, game_id, game_label, tip, market, side, line,"
                  " price, book, fair_prob, book_prob, player_name, details, created_at, "
                  "dedup_key from alerts where signal in ('value','snipe','g1','g2')").fetchall()
n = {}
with eng.connect() as p:
    for (aid, sig, sport, gid, gl, tip, market, side, line, price, book, fair, bp, player, det,
         created, key) in rows:
        d = json.loads(det) if det else {}
        a = SimpleNamespace(signal=sig, sport=sport, game_id=gid, game_label=gl, tip=ts(tip),
                            market=market, side=side, line=line, price=price, book=book,
                            fair_prob=fair, book_prob=bp, player_name=player, details=d,
                            dedup_key=key)
        lab = LB.label(p, a, ts(created))
        if lab:
            d["label"] = lab
            n[lab["label"]] = n.get(lab["label"], 0) + 1
            db.execute("update alerts set details=? where alert_id=?", (json.dumps(d), aid))
db.commit()
print("preview labels", n)
sh("uv", "run", "--no-dev", "betdata", "ledger", "sync")
sh("uv", "run", "--no-dev", "betdata", "page", "build", "--site", "out/preview")
