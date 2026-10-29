"""Spread grades: which agreement signals held at pick time (line move, Pinnacle)

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("nfl_bet_grades", "nba_bet_grades"):
        with op.batch_alter_table(name, schema=None) as batch_op:
            batch_op.add_column(sa.Column("agree", sa.String(length=32), nullable=True))


def downgrade() -> None:
    for name in ("nfl_bet_grades", "nba_bet_grades"):
        with op.batch_alter_table(name, schema=None) as batch_op:
            batch_op.drop_column("agree")
