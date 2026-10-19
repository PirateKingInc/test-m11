"""M13 moneyline: market moneyline prices on predictions; moneyline grades

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_predictions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("market_ml_home", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("market_ml_away", sa.Integer(), nullable=True))
    with op.batch_alter_table("nfl_bet_grades", schema=None) as batch_op:
        batch_op.alter_column("market", existing_type=sa.String(length=8),
                              type_=sa.String(length=16), existing_nullable=False)
        batch_op.alter_column("line", existing_type=sa.Float(), nullable=True)
        batch_op.add_column(sa.Column("close_price_american", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("nfl_bet_grades", schema=None) as batch_op:
        batch_op.drop_column("close_price_american")
        batch_op.alter_column("line", existing_type=sa.Float(), nullable=False)
        batch_op.alter_column("market", existing_type=sa.String(length=16),
                              type_=sa.String(length=8), existing_nullable=False)
    with op.batch_alter_table("model_predictions", schema=None) as batch_op:
        batch_op.drop_column("market_ml_away")
        batch_op.drop_column("market_ml_home")
