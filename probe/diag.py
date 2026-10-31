"""Tracker probe: build the page from the live databases with this branch (out/after) and with
main's whole package (out/before). No secrets are read."""

import subprocess


def sh(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    print("$", " ".join(cmd), "rc", r.returncode, r.stdout[-600:], r.stderr[-1500:])


sh("uv", "run", "--no-dev", "betdata", "page", "build", "--site", "out/after")
sh("git", "fetch", "-q", "--depth", "1", "origin", "main")
sh("git", "checkout", "FETCH_HEAD", "--", "src/betdata")
sh("uv", "run", "--no-dev", "betdata", "page", "build", "--site", "out/before")
