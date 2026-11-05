"""Probe (news alerts): Jetstream reachability from a runner (and the watched DIDs)."""
import subprocess
import sys

for cmd in (["betdata", "news", "stream-check", "--seconds", "20"],
            ["betdata", "news", "stream-check", "--seconds", "45", "--no-all-posts"]):
    r = subprocess.run(["uv", "run", "--no-dev", *cmd], capture_output=True, text=True)
    print("$", " ".join(cmd)); print(r.stdout); print(r.stderr[-3000:], file=sys.stderr)
