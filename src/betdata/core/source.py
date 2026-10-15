"""Shared interface for every ingestion source.

    fetch(job, params) -> ApiResponse   the only step that touches the network (quota-guarded)
    parse(raw)         -> batch         pure; tested against recorded fixtures
    load(conn, batch)  -> stats         writes via shared loaders

`run` wires them together inside an `ingest_runs` row; `reparse` replays a stored raw response
through parse+load without spending quota.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, insert, update

from betdata.config import SourceConfig
from betdata.core import raw_store
from betdata.core.errors import IngestError, NotConfigured, QuotaExceeded, SourceUnavailable
from betdata.db.schema import ingest_runs, utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobSpec:
    name: str
    description: str
    default_priority: str = "normal"


@dataclass
class RunResult:
    run_id: int
    status: str  # ok, failed, skipped
    rows_inserted: int = 0
    rows_seen: int = 0
    message: str = ""


@dataclass(frozen=True)
class RawInput:
    """What `parse` receives, whether fresh from the network or replayed from raw_responses."""

    endpoint: str
    params: dict[str, Any] | None
    body: bytes
    fetched_at: Any
    raw_id: int


class Source(ABC):
    name: str

    def __init__(self, engine: Engine, config: SourceConfig, clock=None):
        self.engine = engine
        self.config = config
        self.clock = clock or utcnow

    @classmethod
    @abstractmethod
    def jobs(cls) -> dict[str, JobSpec]: ...

    def close(self) -> None:
        client = getattr(self, "client", None)
        if client is not None:
            client.close()

    @abstractmethod
    def fetch(
        self, job: str, params: dict[str, Any], *, priority: str, run_id: int
    ) -> RawInput: ...

    @abstractmethod
    def parse(self, job: str, raw: RawInput) -> Any: ...

    @abstractmethod
    def load(
        self, conn, job: str, batch: Any, *, run_id: int | None, raw_id: int
    ) -> tuple[int, int]:
        """Returns (rows_inserted, rows_seen)."""

    def estimate_cost(self, job: str, params: dict[str, Any]) -> int:
        """Quota units one run of `job` is expected to cost (0 for unmetered sources)."""
        return 0

    @abstractmethod
    def job_for_endpoint(self, endpoint: str) -> str:
        """Which job's parser handles a stored raw response (used by reparse)."""

    def run(
        self,
        job: str,
        priority: str | None = None,
        *,
        scheduled_job: str | None = None,
        **params: Any,
    ) -> RunResult:
        spec = self.jobs().get(job)
        if spec is None:
            raise KeyError(f"{self.name} has no job {job!r}; available: {sorted(self.jobs())}")
        priority = priority or spec.default_priority
        with self.engine.begin() as conn:
            run_id = conn.execute(
                insert(ingest_runs).values(
                    source=self.name,
                    job=job,
                    params=params,
                    scheduled_job=scheduled_job,
                    started_at=self.clock(),
                )
            ).inserted_primary_key[0]

        result = RunResult(run_id=run_id, status="failed")
        try:
            raw = self.fetch(job, params, priority=priority, run_id=run_id)
            batch = self.parse(job, raw)
            with self.engine.begin() as conn:
                result.rows_inserted, result.rows_seen = self.load(
                    conn, job, batch, run_id=run_id, raw_id=raw.raw_id
                )
            result.status = "ok"
        except IngestError as e:
            # Expected failure modes (quota, breaker, API error, format change): record and move on.
            result.message = f"{type(e).__name__}: {e}"
            skipped = isinstance(e, QuotaExceeded | SourceUnavailable | NotConfigured)
            result.status = "skipped" if skipped else "failed"
            log.warning("%s.%s %s: %s", self.name, job, result.status, result.message)
        except Exception as e:
            result.message = f"{type(e).__name__}: {e}"
            log.exception("%s.%s crashed", self.name, job)
            raise
        finally:
            with self.engine.begin() as conn:
                conn.execute(
                    update(ingest_runs)
                    .where(ingest_runs.c.run_id == run_id)
                    .values(
                        finished_at=self.clock(),
                        status=result.status,
                        rows_inserted=result.rows_inserted,
                        rows_seen=result.rows_seen,
                        message=result.message or None,
                    )
                )
        return result

    def reparse(self, raw_id: int) -> tuple[int, int]:
        with self.engine.begin() as conn:
            row = raw_store.load(conn, raw_id)
            if row["source"] != self.name:
                raise ValueError(f"raw {raw_id} belongs to {row['source']}, not {self.name}")
            raw = RawInput(row["endpoint"], row["params"], row["body"], row["fetched_at"], raw_id)
            job = self.job_for_endpoint(raw.endpoint)
            return self.load(conn, job, self.parse(job, raw), run_id=None, raw_id=raw_id)
