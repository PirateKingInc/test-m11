"""M12 predictions (point, blend, feature snapshot, factors) and graded bets

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import betdata.db.schema

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_predictions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("prediction_point", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("blend_home_margin", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("blend_total", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("features", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("factors", sa.JSON(), nullable=True))
    op.create_table(
        "nfl_bet_grades",
        sa.Column("grade_id", sa.Integer(), nullable=False),
        sa.Column("prediction_id", sa.Integer(), nullable=False),
        sa.Column("model_version", sa.String(length=128), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("line", sa.Float(), nullable=False),
        sa.Column("price_american", sa.Integer(), nullable=False),
        sa.Column("edge", sa.Float(), nullable=False),
        sa.Column("close_line", sa.Float(), nullable=True),
        sa.Column("clv", sa.Float(), nullable=True),
        sa.Column("result", sa.String(length=8), nullable=False),
        sa.Column("profit", sa.Float(), nullable=False),
        sa.Column("graded_at", betdata.db.schema.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.game_id"],
                                name=op.f("fk_nfl_bet_grades_game_id_games")),
        sa.ForeignKeyConstraint(["prediction_id"], ["model_predictions.prediction_id"],
                                name=op.f("fk_nfl_bet_grades_prediction_id_model_predictions")),
        sa.PrimaryKeyConstraint("grade_id", name=op.f("pk_nfl_bet_grades")),
        sa.UniqueConstraint("prediction_id", "market",
                            name=op.f("uq_nfl_bet_grades_prediction_id_market")),
    )
    with op.batch_alter_table("nfl_bet_grades", schema=None) as batch_op:
        batch_op.create_index("ix_nfl_bet_grades_version", ["model_version"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("nfl_bet_grades", schema=None) as batch_op:
        batch_op.drop_index("ix_nfl_bet_grades_version")
    op.drop_table("nfl_bet_grades")
    with op.batch_alter_table("model_predictions", schema=None) as batch_op:
        for c in ("factors", "features", "blend_total", "blend_home_margin", "prediction_point"):
            batch_op.drop_column(c)
