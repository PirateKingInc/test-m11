"""Venues: one row per stadium/arena, mapped from each source's own venue id."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, insert, select, update

from betdata.db.schema import venue_source_ids, venues

ROOF_TYPES = {"open", "dome", "retractable", None}
FIELDS = ("name", "city", "state", "country", "lat", "lon", "elevation_m", "roof_type",
          "surface", "tz")  # fmt: skip


def venue_for(conn: Connection, source: str, source_venue_id: str) -> int | None:
    return conn.execute(
        select(venue_source_ids.c.venue_id).where(
            venue_source_ids.c.source == source,
            venue_source_ids.c.source_venue_id == str(source_venue_id),
        )
    ).scalar()


def upsert_venue(
    conn: Connection, *, source: str, source_venue_id: str, fill_only: bool = False, **fields: Any
) -> int:
    """Create or update the venue a source id points at.

    `fill_only=True` only fills columns that are still empty (a source that knows less, e.g. a
    game feed with just a name, must not overwrite coordinates from a better one).
    """
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown venue fields {sorted(unknown)}")
    if fields.get("roof_type") not in ROOF_TYPES:
        raise ValueError(f"roof_type must be one of {ROOF_TYPES}, got {fields['roof_type']!r}")
    values = {k: v for k, v in fields.items() if v not in (None, "")}
    venue_id = venue_for(conn, source, source_venue_id)
    if venue_id is None:
        venue_id = conn.execute(
            insert(venues).values(
                name=values.pop("name", None) or f"{source}:{source_venue_id}", **values
            )  # fmt: skip
        ).inserted_primary_key[0]
        conn.execute(
            insert(venue_source_ids).values(
                source=source, source_venue_id=str(source_venue_id), venue_id=venue_id
            )
        )
        return venue_id
    if fill_only:
        current = conn.execute(select(venues).where(venues.c.venue_id == venue_id)).mappings().one()
        values = {k: v for k, v in values.items() if current[k] in (None, "")}
    if values:
        conn.execute(update(venues).where(venues.c.venue_id == venue_id).values(**values))
    return venue_id
