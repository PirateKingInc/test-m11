"""config/news.yaml, and the verified watchlist (config/news_accounts.json)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3] / "config"
PATH = ROOT / "news.yaml"
ACCOUNTS = ROOT / "news_accounts.json"  # written from `betdata news accounts` (verified)


@lru_cache(maxsize=1)
def load(path: Path = PATH) -> dict:
    return yaml.safe_load(path.read_text())


def watch_names() -> list[dict]:
    """Every watchlist entry with its group (national, beat, official)."""
    b = load()["bluesky"]
    return [{**e, "group": g} for g in ("national", "beat", "official") for e in b.get(g, [])]


@dataclass(frozen=True)
class Account:
    name: str
    handle: str
    did: str
    group: str
    team: str | None = None


def accounts(path: Path = ACCOUNTS) -> list[Account]:
    """The verified, active accounts (the account check's output, committed)."""
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    return [Account(a["name"], a["handle"], a["did"], a["group"], a.get("team"))
            for a in raw.get("accounts", []) if a.get("keep")]
