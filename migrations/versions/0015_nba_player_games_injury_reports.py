"""M14 NBA: player box scores and the official injury report (every version)

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import betdata.db.schema

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "nba_player_games",
        sa.Column("player_game_id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.String(length=32), nullable=False),
        sa.Column("player_name", sa.String(length=128), nullable=False),
        sa.Column("starter", sa.Boolean(), nullable=False),
        sa.Column("did_not_play", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=True),
        sa.Column("minutes", sa.Float(), nullable=True),
        sa.Column("points", sa.Float(), nullable=True),
        sa.Column("plus_minus", sa.Float(), nullable=True),
        sa.Column("fgm", sa.Float(), nullable=True),
        sa.Column("fga", sa.Float(), nullable=True),
        sa.Column("fg3m", sa.Float(), nullable=True),
        sa.Column("fg3a", sa.Float(), nullable=True),
        sa.Column("ftm", sa.Float(), nullable=True),
        sa.Column("fta", sa.Float(), nullable=True),
        sa.Column("oreb", sa.Float(), nullable=True),
        sa.Column("dreb", sa.Float(), nullable=True),
        sa.Column("ast", sa.Float(), nullable=True),
        sa.Column("stl", sa.Float(), nullable=True),
        sa.Column("blk", sa.Float(), nullable=True),
        sa.Column("tov", sa.Float(), nullable=True),
        sa.Column("pf", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("known_at", betdata.db.schema.UTCDateTime(), nullable=False),
        sa.Column("fetched_at", betdata.db.schema.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.game_id"],
                                name=op.f("fk_nba_player_games_game_id_games")),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"],
                                name=op.f("fk_nba_player_games_team_id_teams")),
        sa.PrimaryKeyConstraint("player_game_id", name=op.f("pk_nba_player_games")),
        sa.UniqueConstraint("game_id", "player_id",
                            name=op.f("uq_nba_player_games_game_id_player_id")),
    )
    op.create_index("ix_nba_player_games_player", "nba_player_games", ["player_id", "known_at"])
    op.create_table(
        "nba_injury_reports",
        sa.Column("injury_report_id", sa.Integer(), nullable=False),
        sa.Column("game_date", sa.String(length=10), nullable=False),
        sa.Column("matchup", sa.String(length=16), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=True),
        sa.Column("team_id", sa.Integer(), nullable=True),
        sa.Column("team_name", sa.String(length=64), nullable=False),
        sa.Column("player_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=True),
        sa.Column("report_at", betdata.db.schema.UTCDateTime(), nullable=False),
        sa.Column("known_at", betdata.db.schema.UTCDateTime(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", betdata.db.schema.UTCDateTime(), nullable=False),
        sa.Column("raw_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["game_id"], ["games.game_id"],
                                name=op.f("fk_nba_injury_reports_game_id_games")),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"],
                                name=op.f("fk_nba_injury_reports_team_id_teams")),
        sa.ForeignKeyConstraint(["raw_id"], ["raw_responses.raw_id"],
                                name=op.f("fk_nba_injury_reports_raw_id_raw_responses")),
        sa.PrimaryKeyConstraint("injury_report_id", name=op.f("pk_nba_injury_reports")),
        sa.UniqueConstraint("game_date", "matchup", "player_name", "report_at",
                            name=op.f("uq_nba_injury_reports_game_date_matchup_player_name_report_at")),
    )
    op.create_index("ix_nba_injury_game", "nba_injury_reports", ["game_id", "known_at"])


def downgrade() -> None:
    op.drop_index("ix_nba_injury_game", table_name="nba_injury_reports")
    op.drop_table("nba_injury_reports")
    op.drop_index("ix_nba_player_games_player", table_name="nba_player_games")
    op.drop_table("nba_player_games")
