"""M15 player props: prop lines, prop predictions, prop grades

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import betdata.db.schema

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UTC = betdata.db.schema.UTCDateTime


def upgrade() -> None:
    op.create_table(
        "prop_lines",
        sa.Column("prop_line_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_game_id", sa.String(length=32), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=True),
        sa.Column("player_id", sa.String(length=32), nullable=False),
        sa.Column("player_name", sa.String(length=128), nullable=True),
        sa.Column("market", sa.String(length=16), nullable=False),
        sa.Column("book_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("line", sa.Float(), nullable=True),
        sa.Column("over_price", sa.Integer(), nullable=True),
        sa.Column("under_price", sa.Integer(), nullable=True),
        sa.Column("quoted_at", UTC(), nullable=True),
        sa.Column("fetched_at", UTC(), nullable=False),
        sa.Column("raw_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["game_id"], ["games.game_id"],
                                name=op.f("fk_prop_lines_game_id_games")),
        sa.ForeignKeyConstraint(["book_id"], ["books.book_id"],
                                name=op.f("fk_prop_lines_book_id_books")),
        sa.ForeignKeyConstraint(["raw_id"], ["raw_responses.raw_id"],
                                name=op.f("fk_prop_lines_raw_id_raw_responses")),
        sa.PrimaryKeyConstraint("prop_line_id", name=op.f("pk_prop_lines")),
        sa.UniqueConstraint("source", "source_game_id", "player_id", "market", "book_id", "kind",
                            "fetched_at", name=op.f("uq_prop_lines_source")),
    )
    op.create_index("ix_prop_lines_game", "prop_lines",
                    ["game_id", "player_id", "market", "fetched_at"])
    op.create_table(
        "prop_predictions",
        sa.Column("prop_prediction_id", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=32), nullable=False),
        sa.Column("model_version", sa.String(length=128), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.String(length=32), nullable=False),
        sa.Column("player_name", sa.String(length=128), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=True),
        sa.Column("market", sa.String(length=16), nullable=False),
        sa.Column("prediction_point", sa.String(length=16), nullable=False),
        sa.Column("predicted_at", UTC(), nullable=False),
        sa.Column("mean", sa.Float(), nullable=False),
        sa.Column("line", sa.Float(), nullable=True),
        sa.Column("over_price", sa.Integer(), nullable=True),
        sa.Column("under_price", sa.Integer(), nullable=True),
        sa.Column("book_id", sa.Integer(), nullable=True),
        sa.Column("line_fetched_at", UTC(), nullable=True),
        sa.Column("p_over", sa.Float(), nullable=True),
        sa.Column("edge", sa.Float(), nullable=True),
        sa.Column("dist", sa.JSON(), nullable=True),
        sa.Column("factors", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["game_id"], ["games.game_id"],
                                name=op.f("fk_prop_predictions_game_id_games")),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"],
                                name=op.f("fk_prop_predictions_team_id_teams")),
        sa.ForeignKeyConstraint(["book_id"], ["books.book_id"],
                                name=op.f("fk_prop_predictions_book_id_books")),
        sa.PrimaryKeyConstraint("prop_prediction_id", name=op.f("pk_prop_predictions")),
    )
    op.create_index("ix_prop_predictions_game", "prop_predictions",
                    ["game_id", "player_id", "market", "predicted_at"])
    op.create_table(
        "prop_grades",
        sa.Column("grade_id", sa.Integer(), nullable=False),
        sa.Column("prop_prediction_id", sa.Integer(), nullable=False),
        sa.Column("model_version", sa.String(length=128), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.String(length=32), nullable=False),
        sa.Column("market", sa.String(length=16), nullable=False),
        sa.Column("actual", sa.Float(), nullable=True),
        sa.Column("outcome", sa.String(length=8), nullable=False),
        sa.Column("bet_side", sa.String(length=8), nullable=True),
        sa.Column("price_american", sa.Integer(), nullable=True),
        sa.Column("profit", sa.Float(), nullable=True),
        sa.Column("close_line", sa.Float(), nullable=True),
        sa.Column("close_price_american", sa.Integer(), nullable=True),
        sa.Column("clv", sa.Float(), nullable=True),
        sa.Column("graded_at", UTC(), nullable=False),
        sa.ForeignKeyConstraint(["prop_prediction_id"], ["prop_predictions.prop_prediction_id"],
                                name=op.f("fk_prop_grades_prop_prediction_id_prop_predictions")),
        sa.ForeignKeyConstraint(["game_id"], ["games.game_id"],
                                name=op.f("fk_prop_grades_game_id_games")),
        sa.PrimaryKeyConstraint("grade_id", name=op.f("pk_prop_grades")),
        sa.UniqueConstraint("prop_prediction_id", name=op.f("uq_prop_grades_prop_prediction_id")),
    )
    op.create_index("ix_prop_grades_version", "prop_grades", ["model_version", "market"])


def downgrade() -> None:
    op.drop_index("ix_prop_grades_version", table_name="prop_grades")
    op.drop_table("prop_grades")
    op.drop_index("ix_prop_predictions_game", table_name="prop_predictions")
    op.drop_table("prop_predictions")
    op.drop_index("ix_prop_lines_game", table_name="prop_lines")
    op.drop_table("prop_lines")
