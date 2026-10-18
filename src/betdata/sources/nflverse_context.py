"""nflverse team stats, snap counts and weekly rosters: the inputs for unit ratings and for
working out which starters are missing.

  stats_team/stats_team_week_{season}.csv   1999+  team passing/rushing volume and EPA per game
  snap_counts/snap_counts_{season}.csv      2012+  snaps per player per game
  weekly_rosters/roster_weekly_{season}.csv 2002+  roster status per player per week

Each file is one season. These are statistics, not prices, so a reload replaces the season's
rows (nflverse rebuilds them nightly with stat corrections); the raw file is archived as usual.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, delete, insert, select

from betdata.core.errors import ParseError
from betdata.core.matching import sport_id
from betdata.core.normalize.teams import TeamResolver
from betdata.db.schema import game_source_ids, player_game_snaps, roster_weeks, team_game_stats

NAME = "nflverse"

DATASETS = {
    # job: (path template, required columns)
    "team_stats": (
        "/stats_team/stats_team_week_{season}.csv",
        {
            "season",
            "week",
            "team",
            "season_type",
            "game_id",
            "opponent_team",
            "attempts",
            "sacks_suffered",
            "passing_epa",
            "passing_yards",
            "carries",
            "rushing_epa",
            "rushing_yards",
        },
    ),  # fmt: skip
    "snaps": (
        "/snap_counts/snap_counts_{season}.csv",
        {
            "game_id",
            "season",
            "week",
            "player",
            "pfr_player_id",
            "position",
            "team",
            "offense_snaps",
            "offense_pct",
            "defense_snaps",
            "defense_pct",
        },
    ),  # fmt: skip
    "rosters": (
        "/weekly_rosters/roster_weekly_{season}.csv",
        {"season", "week", "team", "status", "full_name", "gsis_id", "pfr_id", "position"},
    ),
}
SKIPPED_ROSTER_STATUSES = {"DEV"}  # practice squad: never a starter


@dataclass
class ParsedFile:
    job: str
    season: int
    fetched_at: datetime
    rows: list[dict]


def _int(v: str) -> int | None:
    return int(float(v)) if v not in ("", "NA") else None


def _float(v: str) -> float | None:
    return float(v) if v not in ("", "NA") else None


def parse(job: str, body: bytes, *, season: int, fetched_at: datetime) -> ParsedFile:
    _, required = DATASETS[job]
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    missing = required - set(reader.fieldnames or [])
    if missing:
        raise ParseError(f"{NAME}: {job} file is missing columns {sorted(missing)}")
    rows = []
    try:
        for r in reader:
            if not (r.get("season") or "").strip():
                continue  # blank line
            if int(r["season"]) != season:
                raise ParseError(f"{NAME}: {job} file for {season} has a {r['season']} row")
            if job == "team_stats":
                rows.append(dict(
                    source_game_id=r["game_id"], season=season, week=int(r["week"]),
                    season_type=r["season_type"] or None, team=r["team"],
                    opponent=r["opponent_team"] or None,
                    pass_plays=(_int(r["attempts"]) or 0) + (_int(r["sacks_suffered"]) or 0),
                    pass_epa=_float(r["passing_epa"]), pass_yards=_int(r["passing_yards"]),
                    rush_plays=_int(r["carries"]) or 0, rush_epa=_float(r["rushing_epa"]),
                    rush_yards=_int(r["rushing_yards"]),
                ))  # fmt: skip
            elif job == "snaps":
                rows.append(dict(
                    source_game_id=r["game_id"], season=season, week=int(r["week"]),
                    team=r["team"], player_name=r["player"], pfr_player_id=r["pfr_player_id"],
                    position=r["position"] or None, offense_snaps=_int(r["offense_snaps"]),
                    offense_pct=_float(r["offense_pct"]), defense_snaps=_int(r["defense_snaps"]),
                    defense_pct=_float(r["defense_pct"]),
                ))  # fmt: skip
            else:
                if r["status"] in SKIPPED_ROSTER_STATUSES or not r["week"]:
                    continue
                rows.append(dict(
                    season=season, week=int(r["week"]), game_type=r.get("game_type") or None,
                    team=r["team"], gsis_id=r["gsis_id"] or None, pfr_id=r["pfr_id"] or None,
                    player_name=r["full_name"], football_name=r.get("football_name") or None,
                    last_name=r.get("last_name") or None, position=r["position"] or None,
                    depth_chart_position=r.get("depth_chart_position") or None,
                    status=r["status"],
                ))  # fmt: skip
    except (KeyError, ValueError, TypeError) as e:
        raise ParseError(f"{NAME}: bad row in {job} file: {e}") from e
    return ParsedFile(job, season, fetched_at, rows)


def load(conn: Connection, batch: ParsedFile, *, raw_id: int | None) -> tuple[int, int]:
    """Replace the season's rows. Rows whose team can't be resolved are skipped (and land in
    the unmatched review queue through the resolver)."""
    teams = TeamResolver(conn, sport_id(conn, "nfl"))
    table = {"team_stats": team_game_stats, "snaps": player_game_snaps,
             "rosters": roster_weeks}[batch.job]  # fmt: skip
    games = {}
    if batch.job != "rosters":
        games = dict(
            conn.execute(
                select(game_source_ids.c.source_game_id, game_source_ids.c.game_id).where(
                    game_source_ids.c.source == NAME
                )
            ).all()
        )
    out, seen_keys = [], set()
    for row in batch.rows:
        row = dict(row)
        team_id = teams.resolve(NAME, row.pop("team"))
        if team_id is None:
            continue
        row.update(team_id=team_id, source=NAME, raw_id=raw_id)
        if batch.job == "team_stats":
            opp = row.pop("opponent")
            row["opponent_team_id"] = teams.resolve(NAME, opp) if opp else None
        if batch.job != "rosters":
            row["game_id"] = games.get(row["source_game_id"])
            key = (row["source_game_id"], team_id, row.get("pfr_player_id"))
            if key in seen_keys:  # the file occasionally repeats a row
                continue
            seen_keys.add(key)
        out.append(row)
    conn.execute(delete(table).where(table.c.source == NAME, table.c.season == batch.season))
    for i in range(0, len(out), 5000):
        conn.execute(insert(table), out[i : i + 5000])
    return len(out), len(batch.rows)
