"""Team name resolution.

Order: exact alias for this source -> exact alias for any source ("*") or canonical name
-> normalized match (this source's aliases, then shared ones). Fuzzy matching only *suggests*
(stored on unmatched_entities); it never links automatically, because college names collide
(Miami vs Miami (OH), USC vs South Carolina). A normalized key shared by two teams is marked
ambiguous and never matches.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from sqlalchemy import Connection, insert, select, update

from betdata.db.schema import team_aliases, teams, unmatched_entities, utcnow

_PUNCT = re.compile(r"[^a-z0-9() ]+")
_SPACES = re.compile(r"\s+")
_AMBIGUOUS = -1


def normalize_name(name: str) -> str:
    s = name.lower().replace("&", " and ").replace("'", "").replace("é", "e")
    s = _PUNCT.sub(" ", s)
    s = re.sub(r"^st\s+", "saint ", s.strip())  # "St. John's" -> "saint johns"
    s = re.sub(r"\s+st$", " state", s)  # "Ohio St." -> "ohio state"
    return _SPACES.sub(" ", s).strip()


@dataclass(frozen=True)
class Suggestion:
    team_id: int
    label: str
    score: float


class TeamResolver:
    def __init__(self, conn: Connection, sport_id: int):
        self.conn = conn
        self.sport_id = sport_id
        self._exact: dict[tuple[str, str], int] = {}
        self._normalized: dict[tuple[str, str], int] = {}
        self._labels: dict[str, int] = {}  # normalized label -> team, for suggestions
        self._display: dict[str, str] = {}  # normalized label -> original text
        self._load()

    def _load(self) -> None:
        for team_id, name in self.conn.execute(
            select(teams.c.team_id, teams.c.canonical_name).where(teams.c.sport_id == self.sport_id)
        ):
            self._exact[("*", name)] = team_id
            self._index("*", name, team_id)
        for source, alias, team_id in self.conn.execute(
            select(team_aliases.c.source, team_aliases.c.alias, team_aliases.c.team_id).where(
                team_aliases.c.sport_id == self.sport_id
            )
        ):
            self._exact.setdefault((source, alias), team_id)
            self._index(source, alias, team_id)

    def _index(self, source: str, name: str, team_id: int) -> None:
        key = normalize_name(name)
        current = self._normalized.get((source, key))
        self._normalized[(source, key)] = team_id if current in (None, team_id) else _AMBIGUOUS
        if source == "*" and len(name) > 4:  # skip abbreviations for suggestions
            self._labels.setdefault(key, team_id)
            self._display.setdefault(key, name)

    def lookup(self, source: str, name: str) -> int | None:
        """Resolve without recording anything."""
        key = normalize_name(name)
        for team_id in (
            self._exact.get((source, name)),
            self._exact.get(("*", name)),
            self._normalized.get((source, key)),
            self._normalized.get(("*", key)),
        ):
            if team_id is not None and team_id != _AMBIGUOUS:
                return team_id
        return None

    def resolve(self, source: str, name: str) -> int | None:
        team_id = self.lookup(source, name)
        if team_id is None:
            self._record_unmatched(source, name)
        return team_id

    def suggest(self, name: str) -> Suggestion | None:
        key = normalize_name(name)
        best = difflib.get_close_matches(key, list(self._labels), n=1, cutoff=0.6)
        if not best:
            return None
        score = difflib.SequenceMatcher(None, key, best[0]).ratio()
        return Suggestion(self._labels[best[0]], self._display[best[0]], round(score, 3))

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
                .values(last_seen=now, times_seen=existing.times_seen + 1, resolved=False)
            )
            return
        suggestion = self.suggest(name)
        self.conn.execute(
            insert(unmatched_entities).values(
                **key,
                suggested_team_id=suggestion.team_id if suggestion else None,
                first_seen=now,
                last_seen=now,
            )
        )
