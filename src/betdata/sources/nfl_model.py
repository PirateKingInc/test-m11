"""Local M12 jobs, so the scheduler can run them like any source (logged in ingest_runs).

Not a data source: `fetch` never touches the network; it only hands the run's clock to
`load`, which rebuilds derived tables, stores live predictions or grades finished games.

  derive   rebuild game info and play-by-play aggregates from stored files
  predict  `betdata predict nfl` for games in the next 8 days (stored change-only)
  grade    `betdata grade nfl`: grade final games, idempotent
  inputs15 refresh the M15 versions' compact live inputs under data/derived (nfl/live_inputs.py)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from betdata.config import PROJECT_ROOT
from betdata.core.errors import NothingToDo
from betdata.core.matching import season_for
from betdata.core.source import JobSpec, RawInput, Source

NAME = "nfl_model"


class NflModelSource(Source):
    name = NAME

    def __init__(self, engine, config, *, transport=None, sleep=None, clock=None):
        # Same signature as the network sources so the scheduler can build any source alike;
        # transport and sleep are unused (no network).
        super().__init__(engine, config, clock)

    @classmethod
    def jobs(cls) -> dict[str, JobSpec]:
        return {
            "derive": JobSpec("derive", "Rebuild derived NFL tables from stored files."),
            "predict": JobSpec("predict", "Store M12 predictions for the next 8 days."),
            "grade": JobSpec("grade", "Grade stored predictions on final games."),
            "inputs15": JobSpec("inputs15", "Refresh m15 and NFL prop live inputs (derived)."),
        }

    def fetch(self, job: str, params: dict[str, Any], *, priority: str, run_id: int) -> RawInput:
        return RawInput(f"local:{job}", params, b"", self.clock(), None)

    def job_for_endpoint(self, endpoint: str) -> str:
        return endpoint.removeprefix("local:")

    def parse(self, job: str, raw: RawInput):
        return raw

    def load(self, conn, job: str, batch: RawInput, *, run_id, raw_id) -> tuple[int, int]:
        now = batch.fetched_at
        if job == "derive":
            from betdata.nfl.derive import derive_game_info, derive_pbp

            data_dir = Path(os.environ.get("BETDATA_DATA_DIR") or PROJECT_ROOT / "data")
            season = season_for("nfl", now)
            return derive_game_info(conn) + derive_pbp(conn, data_dir, [season]), 1
        if job == "inputs15":
            from betdata.nfl.live_inputs import refresh

            data_dir = Path(os.environ.get("BETDATA_DATA_DIR") or PROJECT_ROOT / "data")
            out = refresh(conn, now, data_dir)
            if not out:
                raise NothingToDo("no M15 NFL version to refresh inputs for")
            return sum(out.values()), len(out)
        if job == "grade":
            from betdata.nfl.grade import grade_nfl

            run = grade_nfl(conn, now)
            return run.inserted + run.updated, run.inserted + run.updated + run.pending
        if job == "predict":
            from betdata.nfl.live import run_predict

            days = int((batch.params or {}).get("days", 8))
            try:
                _, picks = run_predict(self.engine, now, days)
            except ValueError as e:  # no history loaded yet
                raise NothingToDo(str(e)) from None
            return sum(p.stored for p in picks), len(picks)
        raise KeyError(job)

