"""M16 evidence: the poller's 30-day quota projection on the live schedule (#162)."""

import subprocess

p = subprocess.run(["uv", "run", "--no-dev", "betdata", "alerts", "quota", "--days", "30"],
                   capture_output=True, text=True)
print(p.returncode, p.stdout[-4000:], p.stderr[-1500:])
