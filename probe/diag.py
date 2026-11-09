"""Probe: the rules v2 diagnostics' database half (read-only state), output to probe-out."""
import shutil
import subprocess
import sys
from pathlib import Path

r = subprocess.run(["uv", "run", "--no-dev", "betdata", "diag", "rules-v2", "--db", "--out",
                    "out"], capture_output=True, text=True)
print(r.stdout[-3000:])
print(r.stderr[-6000:], file=sys.stderr)
Path("out").mkdir(exist_ok=True)
(Path("out") / "stderr.txt").write_text(r.stderr[-20000:])
