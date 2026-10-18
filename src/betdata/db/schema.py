"""Database schema (SQLAlchemy Core).

Portable between SQLite and Postgres: only generic types, surrogate integer keys,
all timestamps stored as UTC. Odds, weather and raw responses are append-only.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql.expression import false
from sqlalchemy.types import TypeDecorator

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


class UTCDateTime(TypeDecorator):
    """Stores naive UTC; always returns tz-aware UTC. Rejects naive input."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; pass a tz-aware UTC datetime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=UTC) if value is not None else None


def utcnow() -> datetime:
    return datetime.now(UTC)


PRICE_STATUSES = ("active", "suspended", "removed")
SNAPSHOT_KINDS = ("poll", "source_opening", "source_closing")

# --- Reference -------------------------------------------------------------

sports = Table(
    "sports",
    metadata,
    Column("sport_id", Integer, primary_key=True),
    Column("key", String(16), nullable=False, unique=True),  # nfl, ncaaf, nba, ncaab
    Column("name", String(64), nullable=False),
)

teams = Table(
    "teams",
    metadata,
    Column("team_id", Integer, primary_key=True),
    Column("sport_id", ForeignKey("sports.sport_id"), nullable=False),
    Column("canonical_name", String(128), nullable=False),
    Column("abbrev", String(16)),
    Column("conference", String(64)),
    Column("division", String(64)),
    Column("division_level", String(16)),  # e.g. FBS, FCS, D1
    Column("active", Boolean, nullable=False, default=True),
    UniqueConstraint("sport_id", "canonical_name"),
)

team_aliases = Table(
    "team_aliases",
    metadata,
    Column("alias_id", Integer, primary_key=True),
    Column("source", String(32), nullable=False),  # "*" = any source
    Column("sport_id", ForeignKey("sports.sport_id"), nullable=False),
    Column("alias", String(128), nullable=False),
    Column("team_id", ForeignKey("teams.team_id"), nullable=False),
    UniqueConstraint("source", "sport_id", "alias"),
)

team_source_ids = Table(
    "team_source_ids",
    metadata,
    Column("source", String(32), primary_key=True),
    Column("sport_id", ForeignKey("sports.sport_id"), primary_key=True),
    Column("source_team_id", String(64), primary_key=True),
    Column("team_id", ForeignKey("teams.team_id"), nullable=False, index=True),
)

unmatched_entities = Table(
    "unmatched_entities",
    metadata,
    Column("unmatched_id", Integer, primary_key=True),
    Column("source", String(32), nullable=False),
    Column("sport_id", ForeignKey("sports.sport_id")),
    Column("kind", String(16), nullable=False),  # team, book, market
    Column("raw_value", String(256), nullable=False),
    Column("suggested_team_id", ForeignKey("teams.team_id")),
    Column("first_seen", UTCDateTime, nullable=False, default=utcnow),
    Column("last_seen", UTCDateTime, nullable=False, default=utcnow),
    Column("times_seen", Integer, nullable=False, default=1),
    Column("resolved", Boolean, nullable=False, default=False),
    UniqueConstraint("source", "sport_id", "kind", "raw_value"),
)

venues = Table(
    "venues",
    metadata,
    Column("venue_id", Integer, primary_key=True),
    Column("name", String(128), nullable=False),
    Column("city", String(64)),
    Column("state", String(32)),
    Column("lat", Float),
    Column("lon", Float),
    Column("elevation_m", Float),
    Column("roof_type", String(16)),  # open, dome, retractable
    Column("surface", String(32)),
    Column("tz", String(64)),
    Column("country", String(2)),  # ISO 3166-1 alpha-2
)

venue_source_ids = Table(
    "venue_source_ids",
    metadata,
    Column("source", String(32), primary_key=True),
    Column("source_venue_id", String(64), primary_key=True),
    Column("venue_id", ForeignKey("venues.venue_id"), nullable=False, index=True),
)

books = Table(
    "books",
    metadata,
    Column("book_id", Integer, primary_key=True),
    Column("key", String(64), nullable=False, unique=True),
    Column("name", String(128), nullable=False),
    Column("region", String(8)),
    Column("is_sharp", Boolean, nullable=False, default=False),
)

book_aliases = Table(
    "book_aliases",
    metadata,
    Column("source", String(32), primary_key=True),
    Column("source_key", String(64), primary_key=True),
    Column("book_id", ForeignKey("books.book_id"), nullable=False),
)

markets = Table(
    "markets",
    metadata,
    Column("market_id", Integer, primary_key=True),
    Column("key", String(32), nullable=False),  # h2h, spreads, totals
    Column("period", String(16), nullable=False, default="game"),  # game, 1h, 2h, q1...
    UniqueConstraint("key", "period"),
)

# --- Games and results -----------------------------------------------------

games = Table(
    "games",
    metadata,
    Column("game_id", Integer, primary_key=True),
    Column("sport_id", ForeignKey("sports.sport_id"), nullable=False),
    Column("season", Integer, nullable=False),
    Column("season_type", String(8)),  # REG, POST, PRE
    Column("week", Integer),
    Column("start_time_utc", UTCDateTime, nullable=False),
    Column("home_team_id", ForeignKey("teams.team_id"), nullable=False),
    Column("away_team_id", ForeignKey("teams.team_id"), nullable=False),
    Column("venue_id", ForeignKey("venues.venue_id")),
    Column("neutral_site", Boolean),
    # Per-game roof state where the source reports it: outdoors, dome, open, closed.
    Column("roof", String(16)),
    Column("status", String(16), nullable=False, default="scheduled"),
    # Source that owns start time/season/week/orientation (e.g. nflverse for NFL).
    Column("schedule_source", String(32)),
    Column("created_at", UTCDateTime, nullable=False, default=utcnow),
    Column("updated_at", UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow),
    Index("ix_games_lookup", "sport_id", "home_team_id", "away_team_id", "start_time_utc"),
)

game_source_ids = Table(
    "game_source_ids",
    metadata,
    Column("source", String(32), primary_key=True),
    Column("source_game_id", String(128), primary_key=True),
    Column("game_id", ForeignKey("games.game_id"), nullable=False, index=True),
    # True when this source's "home" team is the game's away team (neutral sites).
    Column("home_away_swapped", Boolean, nullable=False, default=False, server_default=false()),
)

# Every event a source has listed, with the names it used. Lets odds stored before a team
# name was mapped be linked to games later (`betdata relink`) without re-calling the API.
source_events = Table(
    "source_events",
    metadata,
    Column("source", String(32), primary_key=True),
    Column("source_game_id", String(128), primary_key=True),
    Column("sport_id", ForeignKey("sports.sport_id"), nullable=False),
    Column("home_name", String(128), nullable=False),
    Column("away_name", String(128), nullable=False),
    Column("start_time_utc", UTCDateTime, nullable=False),
    Column("first_seen", UTCDateTime, nullable=False),
    Column("last_seen", UTCDateTime, nullable=False),
)

results = Table(
    "results",
    metadata,
    Column("result_id", Integer, primary_key=True),
    Column("game_id", ForeignKey("games.game_id"), nullable=False),
    Column("source", String(32), nullable=False),
    Column("home_score", Integer),
    Column("away_score", Integer),
    Column("period_scores", JSON),
    Column("final", Boolean, nullable=False, default=False),
    Column("updated_at", UTCDateTime, nullable=False, default=utcnow),
    UniqueConstraint("game_id", "source"),
)

injuries = Table(
    "injuries",
    metadata,
    Column("injury_id", Integer, primary_key=True),
    Column("sport_id", ForeignKey("sports.sport_id"), nullable=False),
    Column("team_id", ForeignKey("teams.team_id")),
    Column("season", Integer),
    Column("week", Integer),
    Column("player_name", String(128), nullable=False),
    Column("source_player_id", String(64)),
    Column("position", String(16)),
    Column("report_status", String(32)),  # game status: Out, Doubtful, Questionable
    Column("practice_status", String(64)),
    Column("detail", String(256)),  # game-report injury
    Column("practice_detail", String(256)),  # practice-report injury
    Column("report_date", UTCDateTime),
    Column("source", String(32), nullable=False),
    Column("fetched_at", UTCDateTime, nullable=False, default=utcnow),
    Column("raw_id", ForeignKey("raw_responses.raw_id")),
    # Append-only like odds: a row per change in a player's weekly status, so Wednesday's
    # "Limited" and Friday's "Out" are both kept.
    Index("ix_injuries_player_week", "source", "season", "week", "source_player_id"),
)

# Large files kept outside the database (play-by-play parquet). One row per version seen; only
# the latest file per season is kept on disk.
data_files = Table(
    "data_files",
    metadata,
    Column("file_id", Integer, primary_key=True),
    Column("source", String(32), nullable=False),
    Column("dataset", String(32), nullable=False),  # e.g. pbp
    Column("season", Integer, nullable=False),
    Column("path", String(512), nullable=False),  # relative to the data directory
    Column("sha256", String(64), nullable=False),
    Column("bytes", Integer, nullable=False),
    Column("url", String(512), nullable=False),
    Column("fetched_at", UTCDateTime, nullable=False),
    Index("ix_data_files_lookup", "source", "dataset", "season", "fetched_at"),
)

# --- Operations (declared before odds/weather, which reference them) --------

raw_responses = Table(
    "raw_responses",
    metadata,
    Column("raw_id", Integer, primary_key=True),
    Column("source", String(32), nullable=False),
    Column("endpoint", String(256), nullable=False),
    Column("params", JSON),  # API keys are stripped before storing
    Column("http_status", Integer, nullable=False),
    Column("fetched_at", UTCDateTime, nullable=False),
    Column("content_type", String(64)),
    Column("sha256", String(64), nullable=False),
    Column("body_gz", LargeBinary, nullable=False),
    Index("ix_raw_source_endpoint_time", "source", "endpoint", "fetched_at"),
)

ingest_runs = Table(
    "ingest_runs",
    metadata,
    Column("run_id", Integer, primary_key=True),
    Column("source", String(32), nullable=False),
    Column("job", String(64), nullable=False),
    Column("params", JSON),
    Column("scheduled_job", String(64)),  # schedule.yaml job id; null for manual runs
    Column("started_at", UTCDateTime, nullable=False, default=utcnow),
    Column("finished_at", UTCDateTime),
    Column("status", String(16), nullable=False, default="running"),  # running, ok, failed, skipped
    Column("rows_inserted", Integer, nullable=False, default=0),
    Column("rows_seen", Integer, nullable=False, default=0),
    Column("message", Text),
)

api_usage = Table(
    "api_usage",
    metadata,
    Column("usage_id", Integer, primary_key=True),
    Column("source", String(32), nullable=False),
    Column("endpoint", String(256), nullable=False),
    Column("called_at", UTCDateTime, nullable=False),
    Column("cost_units", Integer, nullable=False, default=0),
    Column("cost_reported", Boolean, nullable=False, default=False),
    Column("http_status", Integer),  # null when no response (network error)
    Column("remaining_reported", Integer),
    Column("used_reported", Integer),
    Column("latency_ms", Integer),
    Column("attempt", Integer, nullable=False, default=1),
    Column("run_id", ForeignKey("ingest_runs.run_id")),
    Column("error", Text),
    Index("ix_api_usage_source_time", "source", "called_at"),
)

api_quota = Table(
    "api_quota",
    metadata,
    Column("source", String(32), primary_key=True),
    Column("period_start", UTCDateTime, primary_key=True),
    Column("period_end", UTCDateTime, nullable=False),
    Column("limit_units", Integer, nullable=False),
    Column("remaining_reported", Integer),
    Column("used_reported", Integer),
    Column("last_reconciled", UTCDateTime),
)

source_health = Table(
    "source_health",
    metadata,
    Column("source", String(32), primary_key=True),
    Column("consecutive_failures", Integer, nullable=False, default=0),
    Column("open_until", UTCDateTime),  # circuit breaker: skip calls until this time
    Column("last_success", UTCDateTime),
    Column("last_failure", UTCDateTime),
    Column("last_error", Text),
)

# --- Odds (append-only) -----------------------------------------------------

odds_snapshots = Table(
    "odds_snapshots",
    metadata,
    Column("snapshot_id", Integer, primary_key=True),
    Column("source", String(32), nullable=False),
    Column("source_game_id", String(128), nullable=False),
    Column("game_id", ForeignKey("games.game_id")),  # null until teams/game are matched
    Column("book_id", ForeignKey("books.book_id"), nullable=False),
    Column("market_id", ForeignKey("markets.market_id"), nullable=False),
    Column("outcome", String(16), nullable=False),  # home, away, draw, over, under
    Column("line", Float),  # spread/total points; null for moneyline
    Column("price_american", Integer),
    Column("price_decimal", Float),
    Column("price_status", String(16), nullable=False, default="active"),
    Column("snapshot_kind", String(16), nullable=False, default="poll"),
    Column("book_last_update", UTCDateTime),
    Column("fetched_at", UTCDateTime, nullable=False),
    Column("run_id", ForeignKey("ingest_runs.run_id")),
    Column("raw_id", ForeignKey("raw_responses.raw_id")),
    CheckConstraint(f"price_status IN {PRICE_STATUSES}", name="price_status"),
    CheckConstraint(f"snapshot_kind IN {SNAPSHOT_KINDS}", name="snapshot_kind"),
    # One observation per selection per fetch: makes re-parsing a raw response idempotent.
    UniqueConstraint(
        "source", "source_game_id", "book_id", "market_id", "outcome", "snapshot_kind", "fetched_at"
    ),
    Index("ix_odds_game", "game_id", "market_id", "fetched_at"),
)

# --- Weather (append-only) --------------------------------------------------

weather_observations = Table(
    "weather_observations",
    metadata,
    Column("weather_id", Integer, primary_key=True),
    Column("game_id", ForeignKey("games.game_id"), nullable=False),
    Column("source", String(32), nullable=False),
    Column("kind", String(16), nullable=False),  # forecast, observed
    Column("fetched_at", UTCDateTime, nullable=False),
    Column("valid_time", UTCDateTime, nullable=False),
    Column("lead_hours", Float),
    Column("temp_c", Float),
    Column("wind_kph", Float),
    Column("gust_kph", Float),
    Column("wind_dir_deg", Float),
    Column("precip_mm", Float),
    Column("humidity_pct", Float),
    Column("raw_id", ForeignKey("raw_responses.raw_id")),
    UniqueConstraint("game_id", "source", "kind", "fetched_at", "valid_time"),
)

# --- Model output (append-only) ---------------------------------------------

# A model's view of a game before kickoff, with the market line it was compared against.
# Change-only like odds: a new row only when the prediction or the market line moves.
model_predictions = Table(
    "model_predictions",
    metadata,
    Column("prediction_id", Integer, primary_key=True),
    Column("model", String(32), nullable=False),  # e.g. elo
    Column("model_version", String(128), nullable=False),  # model plus its parameters
    Column("game_id", ForeignKey("games.game_id"), nullable=False),
    Column("predicted_at", UTCDateTime, nullable=False),
    Column("pred_home_margin", Float, nullable=False),
    Column("pred_home_win_prob", Float),
    Column("pred_total", Float),  # combined points, for models that predict it
    Column("market_spread_home", Float),  # closing/current line on the home side, if known
    Column("market_total", Float),
    Column("market_source", String(32)),
    Column("params", JSON),
    UniqueConstraint("model_version", "game_id", "predicted_at"),
    Index("ix_model_predictions_game", "game_id", "model_version", "predicted_at"),
)

# --- NFL team and player context (nflverse) -----------------------------------
# Derived stats, not prices: a reload replaces a season's rows (stat corrections), so these
# tables hold the latest version only; the raw files stay in raw_responses.

# One row per team per game: volume and expected points added (EPA) on passes and runs.
team_game_stats = Table(
    "team_game_stats",
    metadata,
    Column("stat_id", Integer, primary_key=True),
    Column("source", String(32), nullable=False),
    Column("source_game_id", String(128), nullable=False),
    Column("game_id", ForeignKey("games.game_id")),
    Column("season", Integer, nullable=False),
    Column("week", Integer, nullable=False),
    Column("season_type", String(8)),
    Column("team_id", ForeignKey("teams.team_id"), nullable=False),
    Column("opponent_team_id", ForeignKey("teams.team_id")),
    Column("pass_plays", Integer, nullable=False),  # attempts + sacks taken
    Column("pass_epa", Float),
    Column("pass_yards", Integer),
    Column("rush_plays", Integer, nullable=False),
    Column("rush_epa", Float),
    Column("rush_yards", Integer),
    Column("raw_id", ForeignKey("raw_responses.raw_id")),
    UniqueConstraint("source", "source_game_id", "team_id"),
    Index("ix_team_game_stats_season", "season", "week"),
)

# Snaps played per player per game (2012+): who the starters are and how much they play.
player_game_snaps = Table(
    "player_game_snaps",
    metadata,
    Column("snap_id", Integer, primary_key=True),
    Column("source", String(32), nullable=False),
    Column("source_game_id", String(128), nullable=False),
    Column("game_id", ForeignKey("games.game_id")),
    Column("season", Integer, nullable=False),
    Column("week", Integer, nullable=False),
    Column("team_id", ForeignKey("teams.team_id"), nullable=False),
    Column("player_name", String(128), nullable=False),
    Column("pfr_player_id", String(32), nullable=False),
    Column("position", String(8)),
    Column("offense_snaps", Integer),
    Column("offense_pct", Float),
    Column("defense_snaps", Integer),
    Column("defense_pct", Float),
    Column("raw_id", ForeignKey("raw_responses.raw_id")),
    UniqueConstraint("source", "source_game_id", "team_id", "pfr_player_id"),
    Index("ix_player_game_snaps_team", "season", "team_id", "week"),
)

# Weekly roster status (ACT active, RES injured reserve/PUP/suspended, INA game-day inactive,
# ...). Practice squad (DEV) rows are not kept. INA is only known 90 minutes before kickoff.
roster_weeks = Table(
    "roster_weeks",
    metadata,
    Column("roster_id", Integer, primary_key=True),
    Column("source", String(32), nullable=False),
    Column("season", Integer, nullable=False),
    Column("week", Integer, nullable=False),
    Column("game_type", String(8)),
    Column("team_id", ForeignKey("teams.team_id"), nullable=False),
    Column("gsis_id", String(32)),
    Column("pfr_id", String(32)),
    Column("player_name", String(128), nullable=False),
    Column("football_name", String(64)),
    Column("last_name", String(64)),
    Column("position", String(8)),
    Column("depth_chart_position", String(8)),
    Column("status", String(8), nullable=False),
    Column("raw_id", ForeignKey("raw_responses.raw_id")),
    Index("ix_roster_weeks_team", "season", "team_id", "week"),
)
