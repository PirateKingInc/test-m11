"""M19 page on the live databases: the review backfill into this runner's (throwaway) ledger
copy, then the page build (out/site) and the weekly report (out/weekly.md). Nothing is pushed
to main; probe-env never saves the caches."""
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine

from betdata import review as RV
from betdata.config import get_settings
from betdata.db.engine import make_engine


def sh(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    print("$", " ".join(cmd), "rc", r.returncode, r.stdout[-400:], r.stderr[-1200:])


peng = create_engine("sqlite:///file:data/poller.sqlite?mode=ro&uri=true", future=True)
t = time.time()
with peng.connect() as p, make_engine(get_settings().db_url).connect() as m:
    print("review", RV.sync(p, m, Path("ledger"), datetime.now(UTC)), f"{time.time() - t:.0f}s")
sh("uv", "run", "--no-dev", "betdata", "page", "build", "--site", "out/site")
from betdata import weekly as W  # noqa: E402

with make_engine(get_settings().db_url).connect() as m, peng.connect() as p:
    md, summary = W.build(Path("."), datetime.now(UTC), m, p)
Path("out/weekly.md").write_text(md)
print("\n".join(summary))
