"""Feature groups in the order they are added (MODEL.md)."""

from __future__ import annotations

from betdata.nfl.features.availability import Availability
from betdata.nfl.features.base import Base
from betdata.nfl.features.coaching import Coaching
from betdata.nfl.features.environment import Environment
from betdata.nfl.features.officials import Officials
from betdata.nfl.features.schedule import Schedule
from betdata.nfl.features.strength import Strength

# MODEL.md order: base, then groups 1-8.
GROUPS = {
    g.name: g
    for g in (
        Base(),
        Availability(),
        Strength(),
        Schedule(),
        Environment(),
        Officials(),
        Coaching(),
    )
}
ORDER = list(GROUPS)


def resolve(names: str) -> list:
    out = []
    for n in names.split(","):
        n = n.strip()
        if n not in GROUPS:
            raise ValueError(f"unknown feature group {n!r}; available: {ORDER}")
        out.append(GROUPS[n])
    if GROUPS["base"] not in out:
        out.insert(0, GROUPS["base"])
    return out


def columns(rows_columns, group: str) -> tuple[list[str], list[str]]:
    m = [c for c in rows_columns if c.startswith(f"m_{group}_")]
    t = [c for c in rows_columns if c.startswith(f"t_{group}_")]
    return m, t
