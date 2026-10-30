"""Page-folds probe: build the page from the live databases with this branch's page code
(out/after) and with main's (out/before). No secrets are read or printed."""

import subprocess

def sh(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    print("$", " ".join(cmd), "rc", r.returncode, r.stdout[-800:], r.stderr[-1500:])

sh("uv", "run", "--no-dev", "betdata", "page", "build", "--site", "out/after")
sh("git", "fetch", "-q", "--depth", "1", "origin", "main")
sh("git", "checkout", "FETCH_HEAD", "--", "src/betdata/page")
sh("uv", "run", "--no-dev", "betdata", "page", "build", "--site", "out/before")
