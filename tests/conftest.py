from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

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
    return copy


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 15, 14, 0, tzinfo=UTC))


@pytest.fixture
def engine(tmp_path):
    url = f"sqlite:///{tmp_path / 'test.sqlite'}"
    upgrade(url)
    eng = make_engine(url)
    seed(eng)
    yield eng
    eng.dispose()


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
