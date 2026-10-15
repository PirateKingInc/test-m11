"""Every API response body is kept (gzipped) so data can be re-parsed without spending quota."""

from __future__ import annotations

import gzip
import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, desc, insert, select

from betdata.db.schema import raw_responses


def save(
    conn: Connection,
    *,
    source: str,
    endpoint: str,
    params: dict[str, Any] | None,
    http_status: int,
    fetched_at: datetime,
    body: bytes,
    content_type: str | None = None,
) -> int:
    result = conn.execute(
        insert(raw_responses).values(
            source=source,
            endpoint=endpoint,
            params=params,
            http_status=http_status,
            fetched_at=fetched_at,
            content_type=content_type,
            sha256=hashlib.sha256(body).hexdigest(),
            body_gz=gzip.compress(body),
        )
    )
    return result.inserted_primary_key[0]


def load(conn: Connection, raw_id: int):
    """Returns the row with `body` (decompressed bytes) added."""
    row = (
        conn.execute(select(raw_responses).where(raw_responses.c.raw_id == raw_id)).mappings().one()
    )
    out = dict(row)
    out["body"] = gzip.decompress(out.pop("body_gz"))
    return out


def find_identical(conn: Connection, source: str, endpoint: str, body: bytes) -> int | None:
    """raw_id of the most recent stored response for this endpoint if its body is identical."""
    latest = conn.execute(
        select(raw_responses.c.raw_id, raw_responses.c.sha256)
        .where(raw_responses.c.source == source, raw_responses.c.endpoint == endpoint)
        .order_by(desc(raw_responses.c.fetched_at), desc(raw_responses.c.raw_id))
        .limit(1)
    ).first()
    if latest and latest.sha256 == hashlib.sha256(body).hexdigest():
        return latest.raw_id
    return None
