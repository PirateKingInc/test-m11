from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text

from betdata.config import PROJECT_ROOT, load_settings
from betdata.db.engine import make_engine, upgrade
from betdata.db.seed import seed

FIXTURES = Path(__file__).parent / "fixtures"


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


class FakeApi:
    """httpx transport that replays queued responses and records requests."""

    def __init__(self):
        self.queue: list[httpx.Response | Exception] = []
        self.requests: list[httpx.Request] = []

    def add(self, status=200, body: bytes | str = b"[]", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.queue.append(httpx.Response(status, content=body, headers=headers or {}))
        return self

    def add_error(self, exc: Exception):
        self.queue.append(exc)
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


@pytest.fixture(autouse=True)
def isolated_seeds(tmp_path, monkeypatch):
    """Aliases added during tests are appended to a copy of seeds/, never the real one."""
    import shutil

    from betdata.db import seed as seed_mod

    copy = tmp_path / "seeds"
    shutil.copytree(seed_mod.SEEDS, copy)
    monkeypatch.setattr(seed_mod, "SEEDS", copy)
    monkeypatch.setenv("BETDATA_DATA_DIR", str(tmp_path / "data"))  # files never land in data/
    return copy


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 15, 14, 0, tzinfo=UTC))


@pytest.fixture
def engine(tmp_path):
    """SQLite by default. With BETDATA_TEST_PG_URL set (e.g.
    postgresql+psycopg://betdata:betdata@localhost/betdata_test) every test gets its own
    Postgres schema, built by the real migrations, and dropped afterwards."""
    pg_url = os.environ.get("BETDATA_TEST_PG_URL")
    if not pg_url:
        url = f"sqlite:///{tmp_path / 'test.sqlite'}"
        upgrade(url)
        eng = make_engine(url)
        seed(eng)
        yield eng
        eng.dispose()
        return

    schema = f"t_{uuid.uuid4().hex[:12]}"
    admin = create_engine(pg_url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = f"{pg_url}{'&' if '?' in pg_url else '?'}options=-csearch_path%3D{schema}"
    try:
        upgrade(url)
        eng = make_engine(url)
        seed(eng)
        yield eng
        eng.dispose()
    finally:
        with admin.begin() as conn:
            # A test that leaks an open connection holds locks on its schema; fail loudly
            # instead of waiting forever, then clean up anyway.
            conn.execute(text("SET LOCAL lock_timeout = '10s'"))
            try:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                leaked = False
            except Exception:  # noqa: BLE001
                leaked = True
        if leaked:
            with admin.begin() as conn:
                conn.execute(text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                    "AND query NOT ILIKE '%DROP SCHEMA%' AND backend_type = 'client backend'"
                ))  # fmt: skip
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
        if leaked:
            pytest.fail("test left a database connection open (schema drop was blocked)")


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-key-123")
    monkeypatch.setenv("CFBD_API_KEY", "cfbd-test-key")
    monkeypatch.setenv("SHARPAPI_KEY", "sharp-test-key")
    monkeypatch.setenv("ODDSPAPI_KEY", "oddspapi-test-key")
    return load_settings(
        config_path=PROJECT_ROOT / "config" / "sources.yaml", env_path=Path("/nonexistent")
    )


@pytest.fixture
def fake_api():
    return FakeApi()


def fixture_bytes(*parts: str) -> bytes:
    return FIXTURES.joinpath(*parts).read_bytes()
