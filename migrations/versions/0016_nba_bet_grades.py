"""M14 NBA: graded paper bets of the NBA model

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import betdata.db.schema

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "nba_bet_grades",
        sa.Column("grade_id", sa.Integer(), nullable=False),
        sa.Column("prediction_id", sa.Integer(), nullable=False),
        sa.Column("model_version", sa.String(length=128), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("market", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("line", sa.Float(), nullable=True),
        sa.Column("price_american", sa.Integer(), nullable=False),
        sa.Column("edge", sa.Float(), nullable=False),
        sa.Column("close_line", sa.Float(), nullable=True),
        sa.Column("clv", sa.Float(), nullable=True),
        sa.Column("close_price_american", sa.Integer(), nullable=True),
        sa.Column("result", sa.String(length=8), nullable=False),
        sa.Column("profit", sa.Float(), nullable=False),
        sa.Column("graded_at", betdata.db.schema.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.game_id"],
                                name=op.f("fk_nba_bet_grades_game_id_games")),
        sa.ForeignKeyConstraint(["prediction_id"], ["model_predictions.prediction_id"],
                                name=op.f("fk_nba_bet_grades_prediction_id_model_predictions")),
        sa.PrimaryKeyConstraint("grade_id", name=op.f("pk_nba_bet_grades")),
        sa.UniqueConstraint("prediction_id", "market",
                            name=op.f("uq_nba_bet_grades_prediction_id_market")),
    )
    op.create_index("ix_nba_bet_grades_version", "nba_bet_grades", ["model_version"])


def downgrade() -> None:
    op.drop_index("ix_nba_bet_grades_version", table_name="nba_bet_grades")
    op.drop_table("nba_bet_grades")
