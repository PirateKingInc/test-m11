"""Team name resolution.

Order: exact alias for this source -> exact alias for any source ("*") -> canonical name
-> normalized match. Fuzzy matching only *suggests* (stored on unmatched_entities); it never
links automatically, because college names collide (Miami (FL) vs Miami (OH)).
"""

from __future__ import annotations

import difflib
import re

from sqlalchemy import Connection, insert, select, update

from betdata.db.schema import team_aliases, teams, unmatched_entities, utcnow

_PUNCT = re.compile(r"[^a-z0-9() ]+")
_SPACES = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    s = name.lower().replace("&", " and ").replace("'", "")
    s = _PUNCT.sub(" ", s)
    s = re.sub(r"^st\s+", "saint ", s.strip())  # "St. John's" -> "saint johns"
    s = re.sub(r"\s+st$", " state", s)  # "Ohio St." -> "ohio state"
    return _SPACES.sub(" ", s).strip()


class TeamResolver:
    def __init__(self, conn: Connection, sport_id: int):
        self.conn = conn
        self.sport_id = sport_id
        self._exact: dict[tuple[str, str], int] = {}
        self._normalized: dict[str, int] = {}
        self._labels: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        for source, alias, team_id in self.conn.execute(
            select(team_aliases.c.source, team_aliases.c.alias, team_aliases.c.team_id).where(
                team_aliases.c.sport_id == self.sport_id
            )
        ):
            self._exact[(source, alias)] = team_id
            self._add_normalized(alias, team_id)
        for team_id, name in self.conn.execute(
            select(teams.c.team_id, teams.c.canonical_name).where(teams.c.sport_id == self.sport_id)
        ):
            self._exact[("*", name)] = team_id
            self._add_normalized(name, team_id)
            self._labels[name] = team_id

    def _add_normalized(self, name: str, team_id: int) -> None:
        key = normalize_name(name)
        # An ambiguous normalized key maps to None so it can never auto-match.
        if key in self._normalized and self._normalized[key] != team_id:
            self._normalized[key] = None  # type: ignore[assignment]
        else:
            self._normalized[key] = team_id

    def resolve(self, source: str, name: str) -> int | None:
        team_id = (
            self._exact.get((source, name))
            or self._exact.get(("*", name))
            or self._normalized.get(normalize_name(name))
        )
        if team_id is None:
            self._record_unmatched(source, name)
        return team_id

    def _record_unmatched(self, source: str, name: str) -> None:
        now = utcnow()
        key = dict(source=source, sport_id=self.sport_id, kind="team", raw_value=name)
        existing = self.conn.execute(
            select(unmatched_entities.c.unmatched_id, unmatched_entities.c.times_seen).where(
                *(unmatched_entities.c[k] == v for k, v in key.items())
            )
        ).first()
        if existing:
            self.conn.execute(
                update(unmatched_entities)
                .where(unmatched_entities.c.unmatched_id == existing.unmatched_id)
                .values(last_seen=now, times_seen=existing.times_seen + 1)
            )
            return
        close = difflib.get_close_matches(name, list(self._labels), n=1, cutoff=0.6)
        self.conn.execute(
            insert(unmatched_entities).values(
                **key,
                suggested_team_id=self._labels[close[0]] if close else None,
                first_seen=now,
                last_seen=now,
            )
        )
