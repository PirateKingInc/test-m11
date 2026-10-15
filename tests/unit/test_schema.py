from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, inspect, select

from betdata.db.schema import metadata, sports, teams


def test_migrations_match_schema_definition(engine):
    """Fails if schema.py changes without a new Alembic migration."""
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), metadata)
    assert diff == []


def test_all_tables_created(engine):
    assert set(metadata.tables) <= set(inspect(engine).get_table_names())


def test_seed_is_idempotent(engine):
    from betdata.db.seed import seed

    seed(engine)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(sports)).scalar() == 4
        assert conn.execute(select(func.count()).select_from(teams)).scalar() == 32


def test_naive_datetimes_rejected(engine):
    from datetime import datetime

    import pytest
    from sqlalchemy.exc import StatementError

    from betdata.db.schema import ingest_runs

    with pytest.raises(StatementError), engine.begin() as conn:
        conn.execute(
            ingest_runs.insert().values(source="x", job="y", started_at=datetime(2026, 1, 1))
        )
