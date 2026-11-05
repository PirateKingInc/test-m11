"""The two official NBA cards, "Next Gen" and "OG" (owner decision 2026-11-05, MODEL.md "Two
official NBA cards"), from config/cards.yaml: names, signals, where each is stored, the OG
record's continuity from Card v1 and its excluded switch-over day, and whether each is active.

`view` turns ledger rows (official.dataset) into one card's official rows, each with its own
`day`, `card_stake` and `card_label`, so every record, goal, day view and CSV works on it."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

PATH = Path(__file__).resolve().parents[2] / "config" / "cards.yaml"
NEXTGEN, OG = "nextgen", "og"
ENGINE_SIGNAL = "nba-engine"


@dataclass(frozen=True)
class Card:
    key: str
    name: str
    signals: tuple
    state: str
    active: bool
    since: str = ""
    history_until: str = ""
    exclude_days: tuple = ()


@lru_cache(maxsize=1)
def load(path: Path = PATH) -> dict[str, Card]:
    raw = yaml.safe_load(path.read_text())["nba"]["cards"]
    return {k: Card(k, v["name"], tuple(v["signals"]), v["state"], bool(v.get("active", True)),
                    str(v.get("since", "")), str(v.get("history_until", "")),
                    tuple(str(d) for d in v.get("exclude_days", [])))
            for k, v in raw.items()}  # fmt: skip


def default() -> str:
    return yaml.safe_load(PATH.read_text())["nba"].get("default", NEXTGEN)


def name(key: str) -> str:
    return load()[key].name


def active(key: str) -> bool:
    return load()[key].active


def og_excluded(r: dict) -> bool:
    """An OG pick left out of the OG record: an excluded day, or annotated og_excluded."""
    return r.get("og_excluded") == "true" or r.get("shadow_date") in load()[OG].exclude_days


def view(rows: list[dict], key: str) -> list[dict]:
    """One NBA card's official rows, each with that card's day, stake and label."""
    c = load()[key]
    out = []
    for r in rows:
        if r["sport"] != "nba":
            continue
        if key == NEXTGEN:
            if r["official"] and r["signal_name"] == ENGINE_SIGNAL:
                out.append({**r, "card_name": c.name})
        else:
            if (r["official"] and r["signal_name"] != ENGINE_SIGNAL
                    and r["day"] <= c.history_until):  # Card v1, Oct 31 - Nov 4
                out.append({**r, "card_name": c.name})
            elif r.get("shadow") and not og_excluded(r):
                out.append({**r, "official": True, "day": r["shadow_date"],
                            "card_date": r["shadow_date"], "card_stake": r["shadow_stake"],
                            "card_label": r["shadow_label"], "card_name": c.name})
    return out


def bet_key(game_id, market, player, side) -> str:
    """The same bet whatever signal found it: game, market, player and side."""
    return f"{game_id}:{market}:{(player or '').lower()}:{side}"


def on_both(rows: list[dict]) -> set[str]:
    """Bet keys on both cards the same day (the same bet chosen by each card's rule)."""
    def keys(v):
        return {(r["day"], bet_key(r["game_id"], r["market"], r["player"], r["side"]))
                for r in v}  # fmt: skip

    return {k for _, k in keys(view(rows, NEXTGEN)) & keys(view(rows, OG))}


def both_mark(r: dict, both: set[str]) -> bool:
    return bet_key(r["game_id"], r["market"], r["player"], r["side"]) in both
