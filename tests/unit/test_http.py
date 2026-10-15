from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from betdata.config import QuotaPolicy
from betdata.core import raw_store
from betdata.core.errors import ApiError, QuotaExceeded, SourceUnavailable
from betdata.core.http import SourceClient
from betdata.core.quota import QuotaManager
from betdata.db.schema import api_usage, source_health


@pytest.fixture
def sleeps():
    return []


def client(engine, fake_api, clock, sleeps, **kw):
    return SourceClient(
        engine,
        "test_src",
        "https://api.example.test",
        transport=fake_api.transport,
        sleep=sleeps.append,
        clock=clock,
        **kw,
    )


def usage_rows(engine):
    with engine.connect() as conn:
        return conn.execute(select(api_usage).order_by(api_usage.c.usage_id)).mappings().all()


def test_success_stores_raw_and_redacts_key(engine, fake_api, clock, sleeps):
    fake_api.add(body=b'{"ok": true}')
    resp = client(engine, fake_api, clock, sleeps).get("/x", {"apiKey": "SECRET", "a": 1})
    with engine.connect() as conn:
        row = raw_store.load(conn, resp.raw_id)
    assert row["body"] == b'{"ok": true}' and row["params"] == {"apiKey": "***", "a": 1}
    assert usage_rows(engine)[0]["cost_units"] == 1


def test_retries_429_honoring_retry_after_then_succeeds(engine, fake_api, clock, sleeps):
    fake_api.add(429, headers={"Retry-After": "7"}).add(503).add(200, b"[]")
    client(engine, fake_api, clock, sleeps).get("/x")
    assert sleeps[0] == 7 and len(sleeps) == 2
    rows = usage_rows(engine)
    assert [r["http_status"] for r in rows] == [429, 503, 200]
    assert [r["cost_units"] for r in rows] == [0, 0, 1]


def test_network_errors_are_retried(engine, fake_api, clock, sleeps):
    fake_api.add_error(httpx.ConnectTimeout("slow")).add(200)
    client(engine, fake_api, clock, sleeps).get("/x")
    assert usage_rows(engine)[0]["http_status"] is None


def test_auth_error_not_retried(engine, fake_api, clock, sleeps):
    fake_api.add(401, b"bad key")
    with pytest.raises(ApiError) as e:
        client(engine, fake_api, clock, sleeps).get("/x")
    assert e.value.status == 401 and len(fake_api.requests) == 1 and sleeps == []


def test_circuit_breaker_opens_and_recovers(engine, fake_api, clock, sleeps):
    c = client(
        engine,
        fake_api,
        clock,
        sleeps,
        max_attempts=1,
        breaker_threshold=2,
        breaker_cooldown=timedelta(hours=1),
    )
    for _ in range(2):
        fake_api.add(500)
        with pytest.raises(ApiError):
            c.get("/x")
    with pytest.raises(SourceUnavailable):
        c.get("/x")
    assert len(fake_api.requests) == 2  # breaker blocked the third call before the network
    clock.advance(hours=1, minutes=1)
    fake_api.add(200)
    c.get("/x")
    with engine.connect() as conn:
        h = conn.execute(select(source_health)).mappings().one()
    assert h["consecutive_failures"] == 0 and h["open_until"] is None


def test_quota_checked_before_network(engine, fake_api, clock, sleeps):
    qm = QuotaManager(engine, "test_src", QuotaPolicy(limit=10, safety_margin=0.0), clock=clock)
    c = client(engine, fake_api, clock, sleeps, quota=qm)
    with pytest.raises(QuotaExceeded):
        c.get("/x", est_cost=11, priority="high")
    assert fake_api.requests == []
