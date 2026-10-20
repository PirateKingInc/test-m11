"""sportsdataverse — ESPN-derived schedules published as GitHub release files.

Same files the `sportsdataverse` package's loaders read (`load_nba_schedule`,
`load_mbb_schedule`), as CSV instead of parquet so no extra dependency is needed:

    espn_nba_schedules/nba_schedule_{label}.csv
    espn_mens_college_basketball_schedules/mbb_schedule_{label}.csv

`label` is the year a season ends (2026-27 -> 2027); our seasons are labelled by the year they
start. Files are rebuilt regularly, so scores lag by up to about a day. No quota.

NBA player box scores (M14): `espn_nba_player_boxscores/player_box_{label}.parquet`, one file
per season (minutes, points, plus/minus, starter, did-not-play), into `nba_player_games`. The
current season has no file until it ends; ESPN's game summaries cover it (`espn_nba`).

Role: a keyless fallback, never the schedule authority. For NBA it creates and scores games
when nba_cdn can't be reached; when nba_cdn works it owns the games and these attach to them.
For college basketball it attaches to CFBD's games through the ESPN game id CFBD recorded, and
teams resolve by ESPN team id (recorded by `teams import --sport ncaab`); games involving a team
without an ESPN id (non-D-I) are skipped.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime
from typing import Any

from betdata.config import SourceConfig
from betdata.core.errors import ConfigError, ParseError
from betdata.core.http import SourceClient
from betdata.core.matching import season_for, sport_id
from betdata.core.models import ParsedEvent, ParsedOdds
from betdata.core.odds_loader import load_odds
from betdata.core.results_loader import load_results
from betdata.core.source import JobSpec, RawInput, Source

NAME = "sportsdataverse"
log = logging.getLogger(__name__)
FILES = {
    "nba": "/espn_nba_schedules/nba_schedule_{label}.csv",
    "ncaab": "/espn_mens_college_basketball_schedules/mbb_schedule_{label}.csv",
}
PLAYER_BOX = "/espn_nba_player_boxscores/player_box_{label}.parquet"
BOX_STATS = {"fgm": "field_goals_made", "fga": "field_goals_attempted",
             "fg3m": "three_point_field_goals_made", "fg3a": "three_point_field_goals_attempted",
             "ftm": "free_throws_made", "fta": "free_throws_attempted",
             "oreb": "offensive_rebounds", "dreb": "defensive_rebounds", "ast": "assists",
             "stl": "steals", "blk": "blocks", "tov": "turnovers", "pf": "fouls"}  # fmt: skip
BOX_COLUMNS = {"game_id", "game_date_time", "athlete_id", "athlete_display_name",
               "team_display_name", "starter", "did_not_play", "reason", "minutes", "points",
               "plus_minus", *BOX_STATS.values()}  # fmt: skip
STATUS = {
    "STATUS_FINAL": "final",
    "STATUS_POSTPONED": "postponed",
    "STATUS_CANCELED": "cancelled",
    "STATUS_CANCELLED": "cancelled",
    "STATUS_SCHEDULED": "scheduled",
}
SEASON_TYPES = {"1": "PRE", "2": "REG", "3": "POST", "5": "POST"}  # 5 = NBA play-in
REQUIRED = {"id", "start_date", "status_type_name", "home_display_name", "away_display_name",
            "home_id", "away_id", "home_score", "away_score"}  # fmt: skip

csv.field_size_limit(1 << 30)  # rows carry a large game_json column


def _int(value: str | None) -> int | None:
    try:
        return int(float(value)) if value not in (None, "", "NA") else None
    except ValueError:
        return None


def _is_franchise(espn_team_id: str) -> bool:
    """ESPN numbers the 30 NBA franchises 1-30; exhibition teams get large ids, TBD none."""
    return espn_team_id.isdigit() and 1 <= int(espn_team_id) <= 30


def parse_schedule(body: bytes, *, sport: str, fetched_at: datetime) -> ParsedOdds:
    reader = csv.DictReader(io.StringIO(body.decode("utf-8")))
    missing = REQUIRED - set(reader.fieldnames or [])
    if missing:
        raise ParseError(f"{NAME}: {sport} schedule is missing columns {sorted(missing)}")
    out = ParsedOdds(
        sport=sport,
        fetched_at=fetched_at,
        team_id_source="espn" if sport == "ncaab" else None,
    )
    for row in reader:
        if sport == "nba" and not (_is_franchise(row["home_id"]) and _is_franchise(row["away_id"])):
            continue  # All-Star exhibitions ("Team Stars", "World") and TBD placeholders
        try:
            start = datetime.fromisoformat(row["start_date"].replace("Z", "+00:00"))
        except ValueError as e:
            raise ParseError(f"{NAME}: bad start_date {row['start_date']!r}") from e
        name = row["status_type_name"]
        season = season_for(sport, start)
        if sport == "nba" and (row.get("season") or "").isdigit():
            # The file's season is the year the season ends; trust it over the calendar, which
            # would put the 2020 bubble games (Jul-Oct 2020) in 2020-21.
            season = int(row["season"]) - 1
        status = STATUS.get(name, "in_progress" if "PROGRESS" in name or "HALF" in name
                            or "END_PERIOD" in name else "scheduled")  # fmt: skip
        home_score, away_score = _int(row["home_score"]), _int(row["away_score"])
        final = status == "final" and home_score is not None and away_score is not None
        out.events.append(
            ParsedEvent(
                source_game_id=row["id"],
                start_time_utc=start,
                home_name=row["home_display_name"],
                away_name=row["away_display_name"],
                neutral_site=(row.get("neutral_site") or "").lower() == "true",
                season=season,
                season_type=SEASON_TYPES.get(row.get("season_type", ""), None),
                home_score=home_score if final else None,
                away_score=away_score if final else None,
                final=final,
                status=status,
                home_source_team_id=row["home_id"] if sport == "ncaab" else None,
                away_source_team_id=row["away_id"] if sport == "ncaab" else None,
                extra_ids={"espn": row["id"]},
            )
        )
    return out


class SportsDataverseSource(Source):
    name = NAME

    def __init__(self, engine, config: SourceConfig, *, transport=None, sleep=None, clock=None):
        super().__init__(engine, config, clock)
        extra = {k: v for k, v in dict(sleep=sleep, clock=clock).items() if v is not None}
        self.client = SourceClient(
            engine,
            NAME,
            config.options.get(
                "base_url",
                "https://github.com/sportsdataverse/sportsdataverse-data/releases/download",
            ),
            transport=transport,
            timeout=120.0,
            **extra,
        )

    @classmethod
    def jobs(cls) -> dict[str, JobSpec]:
        return {
            "schedule": JobSpec(
                "schedule", "ESPN schedule + scores. --sport nba|ncaab --seasons 2026 | 2020-2026"
            ),
            "player_box": JobSpec("player_box", "NBA player box scores (--seasons 2025)"),
        }

    def _seasons(self, params) -> list[int]:
        from betdata.sources.nflverse import parse_seasons

        spec = params.get("seasons")
        if spec == "all":
            raise ConfigError(f"{NAME}: give a season range, e.g. --seasons 2015-2026")
        if spec is None:
            return [season_for(params["sport"], self.clock())]
        return sorted(parse_seasons(spec) or [])

    def fetch(self, job: str, params: dict[str, Any], *, priority: str, run_id: int) -> RawInput:
        if job == "player_box":
            return self._fetch_player_box(params, run_id)
        sport = params.get("sport")
        if sport not in FILES:
            raise ConfigError(f"{NAME}: --sport must be one of {sorted(FILES)}")
        seasons = self._seasons(params)
        if len(seasons) != 1:
            raise ConfigError(
                f"{NAME}: one season per run; for several use "
                "`betdata backfill --source sportsdataverse --sport ... --seasons ...`"
            )
        path = FILES[sport].format(label=seasons[0] + 1)
        resp = self.client.get(path, est_cost=0, run_id=run_id, dedupe=True)
        return RawInput(path, {"sport": sport}, resp.body, resp.fetched_at, resp.raw_id)

    def _fetch_player_box(self, params, run_id) -> RawInput:
        """The season file (~3 MB parquet) is reduced to the columns we keep before archiving,
        so the archive holds a compact JSON copy rather than every file version."""
        from betdata.core import raw_store

        seasons = self._seasons({**params, "sport": "nba"})
        if len(seasons) != 1:
            raise ConfigError(f"{NAME}: player_box takes one season per run")
        path = PLAYER_BOX.format(label=seasons[0] + 1)
        resp = self.client.get(path, est_cost=0, run_id=run_id, store_raw=False)
        body = json.dumps(player_box_rows(resp.body), default=str).encode()
        with self.engine.begin() as conn:
            prev = raw_store.find_identical(conn, NAME, path, body)
            raw_id = prev or raw_store.save(conn, source=NAME, endpoint=path, params=None,
                                            http_status=resp.status, fetched_at=resp.fetched_at,
                                            body=body, content_type="application/json")  # fmt: skip
        return RawInput(path, {"unchanged": prev is not None}, body, resp.fetched_at, raw_id)

    def job_for_endpoint(self, endpoint: str) -> str:
        return "player_box" if "player_box" in endpoint else "schedule"

    def parse(self, job: str, raw: RawInput):
        if job == "player_box":
            rows = json.loads(raw.body)
            for r in rows:
                r["tip"] = datetime.fromisoformat(r["tip"])
            return rows
        sport = (raw.params or {}).get("sport") or (
            "nba" if "nba_schedule" in raw.endpoint else "ncaab"
        )
        return parse_schedule(raw.body, sport=sport, fetched_at=raw.fetched_at)

    def load(self, conn, job: str, batch, *, run_id, raw_id) -> tuple[int, int]:
        if job == "player_box":
            from betdata.nba.box import load_box_rows

            if not batch:
                return 0, 0
            fetched = conn.execute(select_fetched(raw_id)).scalar() if raw_id else None
            n, _ = load_box_rows(conn, batch, source=NAME, fetched_at=fetched or self.clock())
            return n, len(batch)
        if batch.sport == "ncaab":
            batch = _only_espn_known_teams(conn, batch)
        stats = load_odds(conn, source=NAME, batch=batch, run_id=run_id, raw_id=raw_id)
        changed = load_results(conn, source=NAME, events=batch.events, matches=stats.matches)
        return changed, len(batch.events)


def select_fetched(raw_id: int):
    from sqlalchemy import select

    from betdata.db.schema import raw_responses

    return select(raw_responses.c.fetched_at).where(raw_responses.c.raw_id == raw_id)


def _ids(col):
    """ESPN athlete ids as text: integers in most files, floats in some (2025-26)."""
    import pandas as pd

    n = pd.to_numeric(col, errors="coerce")
    return n.map(lambda v: None if v != v else str(int(v)))


def player_box_rows(body: bytes) -> list[dict]:
    """The season parquet as compact rows (ESPN game id, team name, tip, player fields)."""
    import pandas as pd

    try:
        df = pd.read_parquet(io.BytesIO(body))
    except Exception as e:  # pyarrow raises several types for a bad file
        raise ParseError(f"{NAME}: player box file is not a parquet file: {e}") from e
    missing = BOX_COLUMNS - set(df.columns)
    if missing:
        raise ParseError(f"{NAME}: player box file is missing columns {sorted(missing)}")
    tip = pd.to_datetime(df["game_date_time"], utc=True)

    def num(c):
        return pd.to_numeric(df[c], errors="coerce")

    out = pd.DataFrame({
        "espn_game_id": df["game_id"].astype(str), "team": df["team_display_name"].astype(str),
        "tip": tip.map(lambda t: t.isoformat()), "player_id": _ids(df["athlete_id"]),
        "player_name": df["athlete_display_name"].astype(str),
        "starter": df["starter"].fillna(False).astype(bool),
        "did_not_play": df["did_not_play"].fillna(False).astype(bool),
        "reason": df["reason"].where(df["reason"].notna(), None),
        "minutes": num("minutes"), "points": num("points"), "plus_minus": num("plus_minus"),
        **{k: num(c) for k, c in BOX_STATS.items()},
    })  # fmt: skip
    out = out[out.player_id.notna()]
    rows = out.to_dict("records")
    for r in rows:
        for k in ("minutes", "points", "plus_minus", *BOX_STATS):
            if r[k] != r[k]:  # NaN
                r[k] = None
        if r["reason"] is not None:
            r["reason"] = str(r["reason"])[:128]
    return rows


def _only_espn_known_teams(conn, batch: ParsedOdds) -> ParsedOdds:
    from sqlalchemy import select

    from betdata.db.schema import team_source_ids

    known = set(
        conn.execute(
            select(team_source_ids.c.source_team_id).where(
                team_source_ids.c.source == "espn",
                team_source_ids.c.sport_id == sport_id(conn, batch.sport),
            )
        ).scalars()
    )
    if not known:
        raise ConfigError("no ESPN team ids yet; run `betdata teams import --sport ncaab` first")
    before = len(batch.events)
    batch.events = [
        e for e in batch.events if e.home_source_team_id in known and e.away_source_team_id in known
    ]
    if before != len(batch.events):
        log.info(
            "%s ncaab: skipped %d games with untracked teams", NAME, before - len(batch.events)
        )
    return batch
