"""nflverse injuries and play-by-play.

Injuries (`injuries/injuries_{season}.csv`, 2009+): one row per team/week/player holding the
latest practice and game status. Stored change-only per player and week, so the progression
through the week (Wed limited -> Fri out) is kept as it is observed. Run it daily in season.

Play-by-play (`pbp/play_by_play_{season}.parquet`, ~20 MB a season): kept as files under
`<data_dir>/pbp/nfl/`, with each new version recorded in `data_files` (sha256, size, time).
Only the latest file per season stays on disk: nightly rebuilds mostly fix stat corrections
and keeping every version would cost gigabytes a season. Read them with pandas/polars/duckdb.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Connection, desc, insert, select

from betdata.core.errors import ParseError
from betdata.core.matching import sport_id
from betdata.core.normalize.teams import TeamResolver
from betdata.db.schema import data_files, injuries

NAME = "nflverse"
INJURY_COLUMNS = {
    "season",
    "week",
    "team",
    "gsis_id",
    "full_name",
    "position",
    "report_status",
    "practice_status",
    "report_primary_injury",
    "practice_primary_injury",
}
TRACKED = ("team_id", "report_status", "practice_status", "detail", "practice_detail")


def _modified(value: str | None) -> datetime | None:
    """nflverse `date_modified` ('2019-09-06T13:36:21Z'): when the report row was published."""
    if not value or value in ("NA", "\\N"):
        return None
    try:
        t = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _injury_text(primary: str, secondary: str) -> str | None:
    parts = [p.strip() for p in (primary, secondary) if p and p.strip()]
    return ", ".join(parts) or None


@dataclass
class ParsedInjuries:
    season: int
    fetched_at: datetime
    rows: list[dict]


def parse_injuries(body: bytes, *, fetched_at: datetime) -> ParsedInjuries:
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    missing = INJURY_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise ParseError(f"{NAME}: injuries file is missing columns {sorted(missing)}")
    rows, seasons = [], set()
    for r in reader:
        seasons.add(int(r["season"]))
        rows.append(
            dict(
                season=int(r["season"]),
                week=int(r["week"]) if r["week"] else None,
                team=r["team"],
                source_player_id=r["gsis_id"] or None,
                player_name=r["full_name"],
                position=r["position"] or None,
                report_status=r["report_status"] or None,
                practice_status=r["practice_status"] or None,
                detail=_injury_text(
                    r["report_primary_injury"], r.get("report_secondary_injury", "")
                ),
                practice_detail=_injury_text(
                    r["practice_primary_injury"], r.get("practice_secondary_injury", "")
                ),
                report_date=_modified(r.get("date_modified")),
            )
        )
    if len(seasons) > 1:
        raise ParseError(f"{NAME}: injuries file mixes seasons {sorted(seasons)}")
    return ParsedInjuries(seasons.pop() if seasons else 0, fetched_at, rows)


def load_injuries(
    conn: Connection, batch: ParsedInjuries, *, raw_id: int | None
) -> tuple[int, int]:
    """Insert a row only where a player's status for that week changed (or is new)."""
    sid = sport_id(conn, "nfl")
    teams = TeamResolver(conn, sid)
    latest: dict[tuple, dict] = {}
    for r in conn.execute(
        select(injuries)
        .where(
            injuries.c.source == NAME,
            injuries.c.season == batch.season,
            injuries.c.fetched_at <= batch.fetched_at,
        )  # fmt: skip
        .order_by(injuries.c.fetched_at, injuries.c.injury_id)
    ).mappings():
        latest[(r["week"], r["source_player_id"], r["player_name"])] = dict(r)
    new = []
    for row in batch.rows:
        team_id = teams.resolve(NAME, row.pop("team"))
        row = dict(row, team_id=team_id)
        prev = latest.get((row["week"], row["source_player_id"], row["player_name"]))
        if prev and (prev["fetched_at"] == batch.fetched_at
                     or all(prev[k] == row[k] for k in TRACKED)):  # fmt: skip
            continue
        new.append(dict(row, sport_id=sid, source=NAME, fetched_at=batch.fetched_at, raw_id=raw_id))
    if new:
        conn.execute(insert(injuries), new)
    return len(new), len(batch.rows)


# --- play-by-play files ------------------------------------------------------------------


@dataclass
class PbpFile:
    season: int
    url: str
    body: bytes
    fetched_at: datetime


def check_parquet(body: bytes) -> None:
    if len(body) < 12 or body[:4] != b"PAR1" or body[-4:] != b"PAR1":
        raise ParseError(f"{NAME}: play-by-play download is not a parquet file ({len(body)} bytes)")


def store_pbp(conn: Connection, f: PbpFile, data_dir: Path) -> tuple[int, int]:
    """Write the file if its content changed since the last version; record the version."""
    check_parquet(f.body)
    sha = hashlib.sha256(f.body).hexdigest()
    last = conn.execute(
        select(data_files.c.sha256)
        .where(
            data_files.c.source == NAME,
            data_files.c.dataset == "pbp",
            data_files.c.season == f.season,
        )  # fmt: skip
        .order_by(desc(data_files.c.fetched_at), desc(data_files.c.file_id))
        .limit(1)
    ).scalar()
    if last == sha:
        return 0, 1
    rel = Path("pbp") / "nfl" / f"play_by_play_{f.season}.parquet"
    target = data_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".parquet.tmp")
    tmp.write_bytes(f.body)
    os.replace(tmp, target)  # atomic: readers never see a half-written file
    conn.execute(
        insert(data_files).values(
            source=NAME,
            dataset="pbp",
            season=f.season,
            path=rel.as_posix(),
            sha256=sha,
            bytes=len(f.body),
            url=f.url,
            fetched_at=f.fetched_at,
        )  # fmt: skip
    )
    return 1, 1


# --- publish times for rows loaded before report_date was parsed ---------------------------


def fill_report_dates(conn: Connection) -> tuple[int, int]:
    """Fill `injuries.report_date` (nflverse `date_modified`) on rows loaded before it was
    parsed, from the archived raw file each row came from. No network call. Returns
    (rows updated, archived files read)."""
    from sqlalchemy import bindparam, update

    from betdata.core import raw_store

    raw_ids = conn.execute(
        select(injuries.c.raw_id)
        .where(injuries.c.source == NAME, injuries.c.report_date.is_(None),
               injuries.c.raw_id.is_not(None))  # fmt: skip
        .distinct()
    ).scalars().all()
    updated = 0
    for raw_id in raw_ids:
        row = raw_store.load(conn, raw_id)
        if row["source"] != NAME or not row["endpoint"].startswith("/injuries/"):
            continue
        times = {
            (r["week"], r["source_player_id"], r["player_name"]): r["report_date"]
            for r in parse_injuries(row["body"], fetched_at=row["fetched_at"]).rows
            if r["report_date"] is not None
        }
        pending = conn.execute(
            select(injuries.c.injury_id, injuries.c.week, injuries.c.source_player_id,
                   injuries.c.player_name)
            .where(injuries.c.raw_id == raw_id, injuries.c.report_date.is_(None))
        ).all()  # fmt: skip
        values = [
            {"b_id": p.injury_id, "b_date": times[(p.week, p.source_player_id, p.player_name)]}
            for p in pending
            if (p.week, p.source_player_id, p.player_name) in times
        ]
        if values:
            conn.execute(
                update(injuries)
                .where(injuries.c.injury_id == bindparam("b_id"))
                .values(report_date=bindparam("b_date")),
                values,
            )
            updated += len(values)
    return updated, len(raw_ids)
