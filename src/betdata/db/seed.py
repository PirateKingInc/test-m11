"""Loads reference data from seeds/*.csv. Idempotent: existing rows are left alone."""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from sqlalchemy import Connection, Engine, insert, select

from betdata.config import PROJECT_ROOT
from betdata.db.schema import book_aliases, books, markets, sports, team_aliases, teams

SEEDS = PROJECT_ROOT / "seeds"
log = logging.getLogger(__name__)


def _rows(path: Path):
    with path.open(newline="") as f:
        yield from csv.DictReader(f)


def _get_or_insert(conn: Connection, table, pk_col, where: dict, values: dict | None = None) -> int:
    found = conn.execute(
        select(pk_col).where(*(table.c[k] == v for k, v in where.items()))
    ).scalar()
    if found is not None:
        return found
    return conn.execute(insert(table).values(**where, **(values or {}))).inserted_primary_key[0]


def seed(engine: Engine, seeds_dir: Path = SEEDS) -> dict[str, int]:
    counts = {"sports": 0, "teams": 0, "aliases": 0, "aliases_pending": 0, "books": 0, "markets": 0}
    with engine.begin() as conn:
        sport_ids = {}
        for r in _rows(seeds_dir / "sports.csv"):
            sport_ids[r["key"]] = _get_or_insert(
                conn, sports, sports.c.sport_id, {"key": r["key"]}, {"name": r["name"]}
            )
        counts["sports"] = len(sport_ids)

        team_ids: dict[tuple[str, str], int] = {}
        for path in sorted(seeds_dir.glob("teams_*.csv")):
            sport = path.stem.removeprefix("teams_")
            sid = sport_ids[sport]
            for r in _rows(path):
                tid = _get_or_insert(
                    conn,
                    teams,
                    teams.c.team_id,
                    {"sport_id": sid, "canonical_name": r["canonical_name"]},
                    {
                        k: r.get(k) or None
                        for k in ("abbrev", "conference", "division", "division_level")
                    }
                    | {"active": True},
                )
                team_ids[(sport, r["canonical_name"])] = tid
                counts["teams"] += 1
                if r.get("abbrev"):
                    _get_or_insert(
                        conn,
                        team_aliases,
                        team_aliases.c.alias_id,
                        {"source": "*", "sport_id": sid, "alias": r["abbrev"]},
                        {"team_id": tid},
                    )

        alias_path = seeds_dir / "team_aliases.csv"
        if alias_path.exists():
            for r in _rows(alias_path):
                tid = (
                    team_ids.get((r["sport"], r["canonical_name"]))
                    or conn.execute(
                        select(teams.c.team_id).where(
                            teams.c.sport_id == sport_ids[r["sport"]],
                            teams.c.canonical_name == r["canonical_name"],
                        )
                    ).scalar()
                )
                if tid is None:
                    # College teams come from `betdata teams import`; their aliases load on
                    # the next seed after that.
                    log.info("alias %r waits for team %r", r["alias"], r["canonical_name"])
                    counts["aliases_pending"] += 1
                    continue
                _get_or_insert(
                    conn,
                    team_aliases,
                    team_aliases.c.alias_id,
                    {"source": r["source"], "sport_id": sport_ids[r["sport"]], "alias": r["alias"]},
                    {"team_id": tid},
                )
                counts["aliases"] += 1

        for r in _rows(seeds_dir / "books.csv"):
            bid = _get_or_insert(
                conn,
                books,
                books.c.book_id,
                {"key": r["key"]},
                {"name": r["name"], "region": r["region"], "is_sharp": r["is_sharp"] == "1"},
            )
            counts["books"] += 1
            for col, value in r.items():
                if col.endswith("_key") and value:
                    source = col.removesuffix("_key")
                    exists = conn.execute(
                        select(book_aliases.c.book_id).where(
                            book_aliases.c.source == source, book_aliases.c.source_key == value
                        )
                    ).first()
                    if not exists:
                        conn.execute(
                            insert(book_aliases).values(
                                source=source, source_key=value, book_id=bid
                            )
                        )

        from betdata.core.venues import upsert_venue

        counts["venues"] = 0
        for path in sorted(seeds_dir.glob("venues_*.csv")):
            for r in _rows(path):
                id_col = next(c for c in r if c.endswith("_id"))
                upsert_venue(
                    conn,
                    source=id_col.removesuffix("_id"),
                    source_venue_id=r[id_col],
                    **{k: (float(v) if k in ("lat", "lon") and v else v or None)
                       for k, v in r.items() if k != id_col},
                )  # fmt: skip
                counts["venues"] += 1

        for r in _rows(seeds_dir / "markets.csv"):
            _get_or_insert(
                conn, markets, markets.c.market_id, {"key": r["key"], "period": r["period"]}
            )
            counts["markets"] += 1
    return counts
