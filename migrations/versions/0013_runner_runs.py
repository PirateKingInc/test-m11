"""M13 hourly runner: one row per run (status, state rebuilds, per-job outcomes)

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import betdata.db.schema

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runner_runs",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("slot_at", betdata.db.schema.UTCDateTime(), nullable=False),
        sa.Column("started_at", betdata.db.schema.UTCDateTime(), nullable=False),
        sa.Column("finished_at", betdata.db.schema.UTCDateTime(), nullable=True),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("rebuilt", sa.Boolean(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_runner_runs")),
    )
    with op.batch_alter_table("runner_runs", schema=None) as batch_op:
        batch_op.create_index("ix_runner_runs_started", ["started_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("runner_runs", schema=None) as batch_op:
        batch_op.drop_index("ix_runner_runs_started")
    op.drop_table("runner_runs")
