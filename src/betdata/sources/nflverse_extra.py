"""nflverse files for the M15 NFL variables, kept as parquet under `<data_dir>/nflverse/`.

Each file is saved at its release path (`nflverse/pbp_participation/pbp_participation_2024.
parquet`, ...), written atomically (tmp + rename), and each new version is recorded in
`data_files` like play-by-play. Read them with `betdata.nfl.nflverse_files`.

  dataset        release file                                            seasons
  participation  pbp_participation/pbp_participation_{season}.parquet    2016+ (none for 2026 yet)
  ftn            ftn_charting/ftn_charting_{season}.parquet              2022+ (weekly in season)
  pfr_{kind}     pfr_advstats/advstats_week_{kind}_{season}.parquet      2018+ (pass rush rec def)
  depth_charts   depth_charts/depth_charts_{season}.parquet              2001+
  player_stats   stats_player/stats_player_week_{season}.parquet         1999+
  ngs_{kind}     nextgen_stats/ngs_{kind}.parquet (all seasons, 2016+)   (passing receiving rushing)
  players        players/players.parquet (one file)                      M17: positions, draft round

A past season's file is downloaded once and then skipped while it is on disk; the current
season's (and the all-season NGS files) at most once a day. A season nflverse has not published
(404) is skipped, not failed. Participation drops its name/number columns (the gsis ids stay),
which cuts it from ~4.7 to ~1 MB a season. FTN charting is CC-BY-SA 4.0: "FTN Data via
nflverse"; the rest is CC-BY 4.0 (nflverse).
"""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pyarrow.parquet as pq
from sqlalchemy import Connection, desc, insert, select

from betdata.config import PROJECT_ROOT, SourceConfig
from betdata.core.errors import ConfigError, NothingToDo, ParseError
from betdata.core.http import SourceClient
from betdata.core.matching import season_for
from betdata.core.source import JobSpec, RawInput, RunResult, Source
from betdata.db.schema import data_files
from betdata.sources.nflverse import parse_seasons
from betdata.sources.nflverse_data import check_parquet

NAME = "nflverse_extra"
ALL_SEASONS = 0  # data_files.season for the combined NGS files
REFRESH_EVERY = timedelta(hours=23)  # "once a day" with slack for a drifting daily run


@dataclass(frozen=True)
class Dataset:
    path: str  # release path; {season} for per-season files
    first: int | None  # first published season; None = one file for all seasons
    required: frozenset[str]


_GAME_COLS = frozenset({"game_id", "season", "week", "team", "opponent", "pfr_player_id"})
DATASETS: dict[str, Dataset] = {
    "participation": Dataset(
        "pbp_participation/pbp_participation_{season}.parquet", 2016,
        frozenset({"nflverse_game_id", "play_id", "possession_team", "offense_players",
                   "defense_players"}),
    ),
    "ftn": Dataset(
        "ftn_charting/ftn_charting_{season}.parquet", 2022,
        frozenset({"nflverse_game_id", "nflverse_play_id", "season", "week"}),
    ),
    **{f"pfr_{k}": Dataset(f"pfr_advstats/advstats_week_{k}_{{season}}.parquet", 2018,
                           _GAME_COLS) for k in ("pass", "rush", "rec", "def")},
    # Two formats (weekly to 2024, daily from 2025); the loader normalizes them.
    "depth_charts": Dataset(
        "depth_charts/depth_charts_{season}.parquet", 2001, frozenset({"gsis_id"})
    ),
    "player_stats": Dataset(
        "stats_player/stats_player_week_{season}.parquet", 1999,
        frozenset({"player_id", "season", "week", "game_id", "team"}),
    ),
    **{f"ngs_{k}": Dataset(f"nextgen_stats/ngs_{k}.parquet", None,
                           frozenset({"season", "week", "team_abbr", "player_gsis_id"}))
       for k in ("passing", "receiving", "rushing")},
    # M17: player ids, position and draft round (rookie priors); one file, refreshed daily.
    "players": Dataset("players/players.parquet", None,
                       frozenset({"gsis_id", "position", "draft_round", "rookie_season"})),
}  # fmt: skip
PARTICIPATION_DROP = ("old_game_id", "players_on_play", "offense_names", "defense_names",
                      "offense_numbers", "defense_numbers")  # fmt: skip


def data_dir(config: SourceConfig) -> Path:
    return Path(config.options.get("data_dir") or os.environ.get("BETDATA_DATA_DIR")
                or PROJECT_ROOT / "data")  # fmt: skip


def release_path(dataset: str, season: int) -> str:
    return DATASETS[dataset].path.format(season=season)


def _label(dataset: str, season: int) -> str:
    return f"{dataset} {'all seasons' if season == ALL_SEASONS else season}"


@dataclass
class ExtraFile:
    dataset: str
    season: int  # ALL_SEASONS for NGS
    url: str
    body: bytes  # as downloaded (its sha256 identifies the version)
    stored: bytes  # what goes on disk
    fetched_at: datetime


def prepare(dataset: str, season: int, url: str, body: bytes, fetched_at: datetime) -> ExtraFile:
    """Check the download is a parquet file with the expected columns; trim participation."""
    check_parquet(body)
    table = pq.read_table(io.BytesIO(body))
    missing = DATASETS[dataset].required - set(table.column_names)
    if missing:
        raise ParseError(f"{NAME}: {_label(dataset, season)} is missing columns {sorted(missing)}")
    stored = body
    if dataset == "participation":
        keep = [c for c in table.column_names if c not in PARTICIPATION_DROP]
        out = io.BytesIO()
        pq.write_table(table.select(keep), out, compression="zstd")
        stored = out.getvalue()
    return ExtraFile(dataset, season, url, body, stored, fetched_at)


def _last_version(conn: Connection, dataset: str, season: int):
    return conn.execute(
        select(data_files.c.sha256, data_files.c.fetched_at)
        .where(data_files.c.source == NAME, data_files.c.dataset == dataset,
               data_files.c.season == season)  # fmt: skip
        .order_by(desc(data_files.c.fetched_at), desc(data_files.c.file_id))
        .limit(1)
    ).first()


def store(conn: Connection, f: ExtraFile, root: Path) -> tuple[int, int]:
    """Write the file (atomically) and record a version if the download changed or the file
    is missing. Returns (new versions, files seen)."""
    rel = Path("nflverse") / release_path(f.dataset, f.season)
    target = root / rel
    sha = hashlib.sha256(f.body).hexdigest()
    last = _last_version(conn, f.dataset, f.season)
    if last is not None and last.sha256 == sha and target.exists():
        return 0, 1
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(f.stored)
    os.replace(tmp, target)  # readers never see a half-written file
    conn.execute(insert(data_files).values(
        source=NAME, dataset=f.dataset, season=f.season, path=rel.as_posix(), sha256=sha,
        bytes=len(f.stored), url=f.url, fetched_at=f.fetched_at,
    ))  # fmt: skip
    return 1, 1


class NflverseExtraSource(Source):
    name = NAME

    def __init__(self, engine, config: SourceConfig, *,
                 transport: httpx.BaseTransport | None = None, sleep=None, clock=None):  # fmt: skip
        super().__init__(engine, config, clock)
        extra = {k: v for k, v in dict(sleep=sleep, clock=clock).items() if v is not None}
        base = config.options.get("base_url",
                                  "https://github.com/nflverse/nflverse-data/releases/download")
        self.client = SourceClient(engine, NAME, base, transport=transport, timeout=120.0,
                                   **extra)  # fmt: skip

    @classmethod
    def jobs(cls) -> dict[str, JobSpec]:
        out = {d: JobSpec(d, f"nflverse {DATASETS[d].path} (one season; default current)")
               for d in DATASETS}  # fmt: skip
        out["refresh"] = JobSpec("refresh", "Every dataset for the current season.")
        return out

    def run(self, job: str, priority: str | None = None, **params: Any) -> RunResult:
        if job != "refresh":
            return super().run(job, priority, **params)
        params.pop("seasons", None)  # always the current season
        results = [super(NflverseExtraSource, self).run(d, priority, **params) for d in DATASETS]
        failed = [f"{d}: {r.message}" for d, r in zip(DATASETS, results, strict=True)
                  if r.status == "failed"]  # fmt: skip
        return RunResult(results[-1].run_id, "failed" if failed else "ok",
                         sum(r.rows_inserted for r in results), sum(r.rows_seen for r in results),
                         "; ".join(failed))  # fmt: skip

    def _season(self, dataset: str, params) -> int:
        if DATASETS[dataset].first is None:
            return ALL_SEASONS
        seasons = parse_seasons(params.get("seasons"), now=self.clock())
        if not seasons or len(seasons) != 1:
            raise ConfigError(f"{NAME}: one season per run; for several use `betdata backfill "
                              "--source nflverse_extra --dataset ... --seasons ...`")  # fmt: skip
        return seasons.pop()

    def fetch(self, job: str, params: dict[str, Any], *, priority: str, run_id: int) -> RawInput:
        season = self._season(job, params)
        first = DATASETS[job].first
        if first is not None and season < first:
            raise NothingToDo(f"{_label(job, season)}: not published (starts {first})")
        current = season_for("nfl", self.clock())
        target = data_dir(self.config) / "nflverse" / release_path(job, season)
        if target.exists() and not params.get("refetch"):
            with self.engine.connect() as conn:
                last = _last_version(conn, job, season)
            if season not in (ALL_SEASONS, current) or (
                last is not None and self.clock() - last.fetched_at < REFRESH_EVERY
            ):
                raise NothingToDo(f"{_label(job, season)}: already on disk")
        path = "/" + release_path(job, season)
        resp = self.client.get(path, est_cost=0, run_id=run_id, store_raw=False,
                               not_found_ok=True)  # fmt: skip
        if resp.status == 404:
            raise NothingToDo(f"{_label(job, season)}: not published by nflverse (404)")
        return RawInput(path, {"season": season}, resp.body, resp.fetched_at, None)

    def job_for_endpoint(self, endpoint: str) -> str:
        raise ValueError(f"{NAME} keeps no raw responses to reparse")

    def parse(self, job: str, raw: RawInput) -> ExtraFile:
        url = str(self.client._http.base_url).rstrip("/") + raw.endpoint
        return prepare(job, raw.params["season"], url, raw.body, raw.fetched_at)

    def load(self, conn, job: str, batch: ExtraFile, *, run_id, raw_id) -> tuple[int, int]:
        return store(conn, batch, data_dir(self.config))
