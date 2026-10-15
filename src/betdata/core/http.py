"""HTTP client shared by all sources.

Every call goes through `SourceClient.get`, which:
  1. refuses if the source's circuit breaker is open,
  2. checks the quota before each attempt,
  3. retries 429/5xx/network errors with exponential backoff + jitter (honoring Retry-After),
  4. logs every attempt to `api_usage` and reconciles API-reported quota,
  5. stores successful bodies in `raw_responses` before anyone parses them.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from sqlalchemy import Engine, insert, select, update

from betdata.core import raw_store
from betdata.core.errors import ApiError, SourceUnavailable
from betdata.core.quota import QuotaManager
from betdata.db.schema import api_usage, source_health, utcnow

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
SECRET_PARAMS = {"apikey", "api_key", "key", "token", "access_token"}


@dataclass(frozen=True)
class CostReport:
    """What the API itself says about quota, parsed from response headers."""

    cost: int | None = None
    remaining: int | None = None
    used: int | None = None


@dataclass(frozen=True)
class ApiResponse:
    status: int
    headers: httpx.Headers
    body: bytes
    fetched_at: datetime
    raw_id: int
    usage: CostReport

    def json(self) -> Any:
        return json.loads(self.body)


def redact(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if params is None:
        return None
    return {k: ("***" if k.lower() in SECRET_PARAMS else v) for k, v in params.items()}


def _retry_after_seconds(resp: httpx.Response, now: datetime) -> float | None:
    value = resp.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - now).total_seconds())
        except (TypeError, ValueError):
            return None


class SourceClient:
    def __init__(
        self,
        engine: Engine,
        source: str,
        base_url: str,
        *,
        quota: QuotaManager | None = None,
        cost_parser: Callable[[httpx.Response], CostReport] | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_attempts: int = 4,
        base_delay: float = 2.0,
        max_delay: float = 60.0,
        breaker_threshold: int = 5,
        breaker_cooldown: timedelta = timedelta(hours=2),
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utcnow,
        user_agent: str = "betdata/0.1 (personal research)",
    ):
        self.engine = engine
        self.source = source
        self.quota = quota
        self.cost_parser = cost_parser or (lambda _r: CostReport())
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.breaker_threshold = breaker_threshold
        self.breaker_cooldown = breaker_cooldown
        self.sleep = sleep
        self.clock = clock
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": user_agent},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- public ---------------------------------------------------------------

    def get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        est_cost: int = 1,
        priority: str = "normal",
        run_id: int | None = None,
    ) -> ApiResponse:
        self._check_breaker()
        last_error: str = ""
        last_status: int | None = None
        for attempt in range(1, self.max_attempts + 1):
            if self.quota and est_cost > 0:
                self.quota.check(est_cost, priority)
            started = self.clock()
            t0 = time.monotonic()
            try:
                resp = self._http.get(endpoint, params=params)
            except httpx.TransportError as e:
                last_error, last_status = f"{type(e).__name__}: {e}", None
                self._log_usage(
                    endpoint, started, t0, attempt, run_id, None, CostReport(0), last_error
                )
                if attempt < self.max_attempts:
                    self.sleep(self._backoff(attempt))
                continue

            report = self.cost_parser(resp)
            ok = resp.is_success
            cost = report.cost if report.cost is not None else (est_cost if ok else 0)
            err = None if ok else f"HTTP {resp.status_code}: {resp.text[:300]}"
            self._log_usage(
                endpoint,
                started,
                t0,
                attempt,
                run_id,
                resp.status_code,
                CostReport(cost, report.remaining, report.used),
                err,
                cost_reported=report.cost is not None,
            )
            if self.quota:
                self.quota.reconcile(report.remaining, report.used)

            if ok:
                with self.engine.begin() as conn:
                    raw_id = raw_store.save(
                        conn,
                        source=self.source,
                        endpoint=endpoint,
                        params=redact(params),
                        http_status=resp.status_code,
                        fetched_at=started,
                        body=resp.content,
                        content_type=resp.headers.get("content-type"),
                    )
                self._record_success()
                return ApiResponse(
                    resp.status_code, resp.headers, resp.content, started, raw_id, report
                )

            last_error, last_status = err, resp.status_code
            if resp.status_code not in RETRYABLE_STATUS:
                break
            if attempt < self.max_attempts:
                wait = _retry_after_seconds(resp, self.clock())
                self.sleep(
                    min(self.max_delay, wait) if wait is not None else self._backoff(attempt)
                )

        self._record_failure(last_error)
        raise ApiError(f"{self.source} {endpoint} failed: {last_error}", status=last_status)

    # -- internals ------------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        # Full jitter: uniform(0, base * 2^(attempt-1)), capped.
        return random.uniform(0, min(self.max_delay, self.base_delay * 2 ** (attempt - 1)))

    def _log_usage(
        self, endpoint, started, t0, attempt, run_id, status, report, error, cost_reported=False
    ):
        with self.engine.begin() as conn:
            conn.execute(
                insert(api_usage).values(
                    source=self.source,
                    endpoint=endpoint,
                    called_at=started,
                    cost_units=report.cost or 0,
                    cost_reported=cost_reported,
                    http_status=status,
                    remaining_reported=report.remaining,
                    used_reported=report.used,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    attempt=attempt,
                    run_id=run_id,
                    error=error,
                )
            )

    def _health(self, conn):
        row = (
            conn.execute(select(source_health).where(source_health.c.source == self.source))
            .mappings()
            .first()
        )
        if row is None:
            conn.execute(insert(source_health).values(source=self.source, consecutive_failures=0))
            return {"consecutive_failures": 0, "open_until": None}
        return row

    def _check_breaker(self) -> None:
        with self.engine.begin() as conn:
            h = self._health(conn)
        if h["open_until"] and h["open_until"] > self.clock():
            raise SourceUnavailable(
                f"{self.source}: circuit open until {h['open_until'].isoformat()} after "
                f"{h['consecutive_failures']} consecutive failures"
            )

    def _record_success(self) -> None:
        with self.engine.begin() as conn:
            self._health(conn)
            conn.execute(
                update(source_health)
                .where(source_health.c.source == self.source)
                .values(consecutive_failures=0, open_until=None, last_success=self.clock())
            )

    def _record_failure(self, error: str) -> None:
        now = self.clock()
        with self.engine.begin() as conn:
            failures = self._health(conn)["consecutive_failures"] + 1
            values = dict(consecutive_failures=failures, last_failure=now, last_error=error[:1000])
            if failures >= self.breaker_threshold:
                values["open_until"] = now + self.breaker_cooldown
            conn.execute(
                update(source_health).where(source_health.c.source == self.source).values(**values)
            )
