"""Card probe: build the page from the live databases with this branch (out/after) and with
main's page code (out/before), then a PREVIEW in this throwaway runner only: today's NBA card
chosen now from the live picks (out/preview). Nothing is saved back."""

import subprocess
from datetime import UTC, datetime, timedelta


def sh(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    print("$", " ".join(cmd), "rc", r.returncode, r.stdout[-600:], r.stderr[-1500:])


sh("uv", "run", "--no-dev", "betdata", "page", "build", "--site", "out/after")
sh("git", "fetch", "-q", "--depth", "1", "origin", "main")
sh("git", "checkout", "FETCH_HEAD", "--", "src/betdata/page")
sh("uv", "run", "--no-dev", "betdata", "page", "build", "--site", "out/before")
sh("git", "checkout", "HEAD", "--", "src/betdata/page")

from betdata.alerts import card as CD  # noqa: E402
from betdata.alerts.db import engine_for, set_state  # noqa: E402
from betdata.alerts.rules import EASTERN  # noqa: E402
from betdata.config import get_settings  # noqa: E402
from betdata.db.engine import make_engine  # noqa: E402

now = datetime.now(UTC)
day = now.astimezone(EASTERN).date()
with make_engine(get_settings().db_url).connect() as m:
    ft = CD.first_tip(m, "nba", day)
at = now if ft is None else min(now, ft - timedelta(minutes=30))
print("first tip", ft, "chosen at", at)
pdb = engine_for("data/poller.sqlite")
with pdb.begin() as p:
    c = CD.choose(p, "nba", day, at)
    set_state(p, CD.key("nba", day), c, at)
print("preview card", [(x["dedup_key"][:40], x["label"], round(x["ev"], 3)) for x in c["picks"]],
      "live", c["live"])
sh("uv", "run", "--no-dev", "betdata", "ledger", "sync")
sh("uv", "run", "--no-dev", "betdata", "page", "build", "--site", "out/preview")
