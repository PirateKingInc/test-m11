"""NFL bet grades: the book a pick was taken at and the book of its close (DK/FD, Pinnacle)

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("nfl_bet_grades", schema=None) as batch_op:
        batch_op.add_column(sa.Column("book", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("close_book", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("nfl_bet_grades", schema=None) as batch_op:
        batch_op.drop_column("close_book")
        batch_op.drop_column("book")
