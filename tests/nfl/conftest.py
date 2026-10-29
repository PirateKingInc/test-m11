import shutil
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert

from betdata.db.schema import data_files
from betdata.sources.nflverse import NflverseSource
from tests.conftest import FIXTURES, fixture_bytes


@pytest.fixture
def loaded(engine, settings, fake_api, clock, tmp_path):
    nfl = NflverseSource(engine, settings.source("nflverse"), transport=fake_api.transport,
                         sleep=lambda s: None, clock=clock)  # fmt: skip
    fake_api.add(body=fixture_bytes("nflverse", "games_sample.csv"))
    assert nfl.run("schedules", seasons="all").status == "ok"
    rel = "pbp/nfl/play_by_play_2025.parquet"
    (tmp_path / "pbp" / "nfl").mkdir(parents=True)
    shutil.copy(FIXTURES / "nflverse" / "pbp_2025_sample.parquet", tmp_path / rel)
    with engine.begin() as conn:
        conn.execute(insert(data_files).values(
            source="nflverse", dataset="pbp", season=2025, path=rel, sha256="x", bytes=1,
            url="u", fetched_at=datetime(2026, 1, 1, tzinfo=UTC)))  # fmt: skip
    return tmp_path


# Shared by the grading and agreement tests: the sample schedule loaded, and a poller database
# (Pinnacle's quotes) where grading looks for it.
@pytest.fixture
def db(engine, settings, fake_api, clock):
    nfl = NflverseSource(engine, settings.source("nflverse"), transport=fake_api.transport,
                         sleep=lambda s: None, clock=clock)  # fmt: skip
    fake_api.add(body=fixture_bytes("nflverse", "games_sample.csv"))
    assert nfl.run("schedules", seasons="all").status == "ok"
    return engine


@pytest.fixture
def poller(tmp_path, monkeypatch):
    from betdata.alerts.db import engine_for

    monkeypatch.setenv("BETDATA_DATA_DIR", str(tmp_path))
    return engine_for(tmp_path / "poller.sqlite")
