"""M13 weather: game weather as it could be known at each prediction point

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import betdata.db.schema

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "nfl_weather_asof",
        sa.Column("weather_asof_id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("point", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("lead_days", sa.Integer(), nullable=True),
        sa.Column("temp_c", sa.Float(), nullable=True),
        sa.Column("wind_kph", sa.Float(), nullable=True),
        sa.Column("gust_kph", sa.Float(), nullable=True),
        sa.Column("precip_mm", sa.Float(), nullable=True),
        sa.Column("known_at", betdata.db.schema.UTCDateTime(), nullable=False),
        sa.Column("fetched_at", betdata.db.schema.UTCDateTime(), nullable=False),
        sa.Column("raw_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["game_id"], ["games.game_id"],
                                name=op.f("fk_nfl_weather_asof_game_id_games")),
        sa.ForeignKeyConstraint(["raw_id"], ["raw_responses.raw_id"],
                                name=op.f("fk_nfl_weather_asof_raw_id_raw_responses")),
        sa.PrimaryKeyConstraint("weather_asof_id", name=op.f("pk_nfl_weather_asof")),
        sa.UniqueConstraint("game_id", "point", "source",
                            name=op.f("uq_nfl_weather_asof_game_id_point_source")),
    )


def downgrade() -> None:
    op.drop_table("nfl_weather_asof")
