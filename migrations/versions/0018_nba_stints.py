"""M15 NBA lineup stints from play-by-play

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import betdata.db.schema

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "nba_stints",
        sa.Column("stint_id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("start_sec", sa.Float(), nullable=False),
        sa.Column("end_sec", sa.Float(), nullable=False),
        sa.Column("home_players", sa.String(length=64), nullable=False),
        sa.Column("away_players", sa.String(length=64), nullable=False),
        sa.Column("home_pts", sa.Integer(), nullable=False),
        sa.Column("away_pts", sa.Integer(), nullable=False),
        sa.Column("home_poss", sa.Float(), nullable=False),
        sa.Column("away_poss", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("known_at", betdata.db.schema.UTCDateTime(), nullable=False),
        sa.Column("fetched_at", betdata.db.schema.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.game_id"],
                                name=op.f("fk_nba_stints_game_id_games")),
        sa.PrimaryKeyConstraint("stint_id", name=op.f("pk_nba_stints")),
        sa.UniqueConstraint("game_id", "period", "start_sec",
                            name=op.f("uq_nba_stints_game_id_period_start_sec")),
    )
    op.create_index("ix_nba_stints_known", "nba_stints", ["known_at"])


def downgrade() -> None:
    op.drop_index("ix_nba_stints_known", table_name="nba_stints")
    op.drop_table("nba_stints")
