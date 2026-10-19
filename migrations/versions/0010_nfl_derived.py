"""M12 derived NFL data: game info and play-by-play aggregates with known_at

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import betdata.db.schema

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "nfl_game_info",
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("source_game_id", sa.String(length=128), nullable=False),
        sa.Column("div_game", sa.Boolean(), nullable=True),
        sa.Column("surface", sa.String(length=32), nullable=True),
        sa.Column("roof", sa.String(length=16), nullable=True),
        sa.Column("home_coach", sa.String(length=64), nullable=True),
        sa.Column("away_coach", sa.String(length=64), nullable=True),
        sa.Column("referee", sa.String(length=64), nullable=True),
        sa.Column("raw_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["game_id"], ["games.game_id"],
                                name=op.f("fk_nfl_game_info_game_id_games")),
        sa.ForeignKeyConstraint(["raw_id"], ["raw_responses.raw_id"],
                                name=op.f("fk_nfl_game_info_raw_id_raw_responses")),
        sa.PrimaryKeyConstraint("game_id", name=op.f("pk_nfl_game_info")),
    )
    op.create_table(
        "nfl_team_game_pbp",
        sa.Column("row_id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("opponent_team_id", sa.Integer(), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=False),
        sa.Column("known_at", betdata.db.schema.UTCDateTime(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["game_id"], ["games.game_id"],
                                name=op.f("fk_nfl_team_game_pbp_game_id_games")),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"],
                                name=op.f("fk_nfl_team_game_pbp_team_id_teams")),
        sa.ForeignKeyConstraint(["opponent_team_id"], ["teams.team_id"],
                                name=op.f("fk_nfl_team_game_pbp_opponent_team_id_teams")),
        sa.ForeignKeyConstraint(["file_id"], ["data_files.file_id"],
                                name=op.f("fk_nfl_team_game_pbp_file_id_data_files")),
        sa.PrimaryKeyConstraint("row_id", name=op.f("pk_nfl_team_game_pbp")),
        sa.UniqueConstraint("game_id", "team_id",
                            name=op.f("uq_nfl_team_game_pbp_game_id_team_id")),
    )
    with op.batch_alter_table("nfl_team_game_pbp", schema=None) as batch_op:
        batch_op.create_index("ix_nfl_team_game_pbp_season", ["season"], unique=False)

    op.create_table(
        "nfl_player_game_pbp",
        sa.Column("row_id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("gsis_id", sa.String(length=32), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=False),
        sa.Column("known_at", betdata.db.schema.UTCDateTime(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["game_id"], ["games.game_id"],
                                name=op.f("fk_nfl_player_game_pbp_game_id_games")),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"],
                                name=op.f("fk_nfl_player_game_pbp_team_id_teams")),
        sa.ForeignKeyConstraint(["file_id"], ["data_files.file_id"],
                                name=op.f("fk_nfl_player_game_pbp_file_id_data_files")),
        sa.PrimaryKeyConstraint("row_id", name=op.f("pk_nfl_player_game_pbp")),
        sa.UniqueConstraint("game_id", "team_id", "gsis_id",
                            name=op.f("uq_nfl_player_game_pbp_game_id_team_id_gsis_id")),
    )
    with op.batch_alter_table("nfl_player_game_pbp", schema=None) as batch_op:
        batch_op.create_index("ix_nfl_player_game_pbp_season", ["season"], unique=False)


def downgrade() -> None:
    for name in ("nfl_player_game_pbp", "nfl_team_game_pbp"):
        with op.batch_alter_table(name, schema=None) as batch_op:
            batch_op.drop_index(f"ix_{name}_season")
        op.drop_table(name)
    op.drop_table("nfl_game_info")
