"""The M16 alert layer's own database (`data/poller.sqlite`, MODEL.md "M16").

The layer never writes the hourly state: the poller works on a throwaway copy of it and keeps
everything it learns here (status changes, polled quotes, re-predictions, alerts and their
grades). The file is saved under its own Actions cache key, and the hourly page only reads it,
so the page keeps working exactly as before if this layer fails or the file is lost.

Tables are created with `create_all` (this file is not part of the Alembic chain of the main
database). Append-only, except the alert's delivery fields and the grade, which is replaced
when a later grading run has more information (a result after a close).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Engine,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)

from betdata.db.schema import UTCDateTime

metadata = MetaData()
DEFAULT_PATH = Path("data/poller.sqlite")

# One poller job (one clock hour): when it ran, what it did, what it spent.
poller_runs = Table(
    "poller_runs",
    metadata,
    Column("run_id", Integer, primary_key=True),
    Column("started_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
    Column("status", String(16), nullable=False, default="running"),  # running, ok, failed
    Column("cycles", Integer, nullable=False, default=0),
    Column("skipped_cycles", Integer, nullable=False, default=0),  # hourly run in progress
    Column("requests", JSON),  # {source: n}
    Column("message", Text),
)

# A player's status change on a report (NBA official report; NFL injury/inactive list), as the
# poller first read it, with the value test of MODEL.md "Status changes that qualify".
status_events = Table(
    "status_events",
    metadata,
    Column("event_id", Integer, primary_key=True),
    Column("sport", String(8), nullable=False),
    Column("game_id", Integer, nullable=False),  # the main database's game id
    Column("player_key", String(64), nullable=False),  # the sport's player id
    Column("player_name", String(128), nullable=False),
    Column("team_id", Integer),
    Column("old_status", String(32)),
    Column("new_status", String(32)),
    Column("prob_before", Float),
    Column("prob_after", Float),
    Column("weight", Float),  # NBA min_play; NFL snap share
    Column("position", String(8)),
    Column("qualifies", Boolean, nullable=False),
    Column("report_name", String(128)),
    Column("report_at", UTCDateTime),  # the report's stated time
    Column("detected_at", UTCDateTime, nullable=False),  # when the poller read it
    Column("tip", UTCDateTime),
    Column("details", JSON),
    UniqueConstraint("sport", "game_id", "player_key", "report_at", "new_status"),
)

# Every price the poller read (DK/FD from SharpAPI, Pinnacle from The Odds API, Kalshi), whether
# for a lag window, a signal or a pre-game close. `event_id` ties a poll to the status change
# whose 60-minute lag window it belongs to (null for other polls).
quotes = Table(
    "quotes",
    metadata,
    Column("quote_id", Integer, primary_key=True),
    Column("polled_at", UTCDateTime, nullable=False),
    Column("sport", String(8), nullable=False),
    Column("game_id", Integer, nullable=False),
    Column("event_id", ForeignKey("status_events.event_id")),
    Column("source", String(32), nullable=False),  # sharpapi, the_odds_api, kalshi
    Column("book", String(32), nullable=False),  # draftkings, fanduel, pinnacle, kalshi
    Column("market", String(32), nullable=False),  # spreads, totals, h2h, or a prop market
    Column("player_name", String(128)),  # props only
    Column("outcome", String(16), nullable=False),  # home, away, over, under
    Column("line", Float),
    Column("price", Integer),  # American odds (Kalshi: the implied price of the yes ask)
    Column("book_updated_at", UTCDateTime),
    Index("ix_quotes_game", "game_id", "market", "polled_at"),
    Index("ix_quotes_event", "event_id"),
)

# A game's re-prediction after a status change, with the frozen page models.
repredictions = Table(
    "repredictions",
    metadata,
    Column("repred_id", Integer, primary_key=True),
    Column("event_id", ForeignKey("status_events.event_id"), nullable=False),
    Column("game_id", Integer, nullable=False),
    Column("model_version", String(64), nullable=False),
    Column("point", String(16)),
    Column("margin_before", Float),
    Column("margin_after", Float),
    Column("total_before", Float),
    Column("total_after", Float),
    Column("props", JSON),  # [{player, market, mean_before, mean_after}]
    Column("created_at", UTCDateTime, nullable=False),
)

# Every alert is a 1-unit paper bet at the alerted price and line (pushed or quiet).
alerts = Table(
    "alerts",
    metadata,
    Column("alert_id", Integer, primary_key=True),
    Column("signal", String(8), nullable=False),  # news, sharp
    Column("sport", String(8), nullable=False),
    Column("game_id", Integer, nullable=False),
    Column("game_label", String(64)),
    Column("tip", UTCDateTime),
    Column("market", String(32), nullable=False),
    Column("player_name", String(128)),
    Column("side", String(16), nullable=False),  # home, away, over, under
    Column("line", Float),
    Column("price", Integer, nullable=False),
    Column("book", String(32), nullable=False),
    Column("fair_prob", Float),  # model or Pinnacle probability of the side
    Column("book_prob", Float),  # the book's no-vig probability of the side
    Column("reason", Text, nullable=False),
    Column("dedup_key", String(255), nullable=False, unique=True),
    Column("event_id", ForeignKey("status_events.event_id")),
    Column("study", Boolean, nullable=False, default=False),  # Signal 1 during the lag study
    Column("created_at", UTCDateTime, nullable=False),
    Column("quiet", Boolean, nullable=False, default=False),
    Column("sent", Boolean, nullable=False, default=False),
    Column("sent_at", UTCDateTime),
    Column("send_status", String(64)),
    Column("page_url", String(255)),
    Column("details", JSON),
)

# The grade of one alert against the pre-game close (MODEL.md "Grading and success rule").
alert_grades = Table(
    "alert_grades",
    metadata,
    Column("alert_id", ForeignKey("alerts.alert_id"), primary_key=True),
    Column("close_line", Float),
    Column("close_price", Integer),
    Column("close_book", String(32)),
    Column("close_at", UTCDateTime),
    Column("clv_points", Float),
    Column("clv_novig", Float),
    Column("result", String(8)),  # win, loss, push, void; null until the game is final
    Column("profit", Float),
    Column("graded_at", UTCDateTime, nullable=False),
)

# Small persistent values (last report read, open Signal 2 gaps, the Signal 1 decision).
state = Table(
    "poller_state",
    metadata,
    Column("key", String(128), primary_key=True),
    Column("value", JSON),
    Column("updated_at", UTCDateTime),
)


def engine_for(path: Path | str = DEFAULT_PATH) -> Engine:
    """An engine on the poller database, created if missing. One file (no WAL), so the Actions
    cache saves exactly what is on disk."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=DELETE")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    metadata.create_all(engine)
    return engine


def get_state(conn, key: str, default=None):
    row = conn.execute(state.select().where(state.c.key == key)).first()
    return default if row is None else row.value


def set_state(conn, key: str, value, now) -> None:
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    stmt = sqlite_insert(state).values(key=key, value=value, updated_at=now)
    conn.execute(stmt.on_conflict_do_update(index_elements=["key"],
                                            set_={"value": value, "updated_at": now}))  # fmt: skip


# M18 Part E: the first price ever seen for each (source, book, game, market, player, side),
# written once and never overwritten (MODEL.md "M18 Part E").
openers = Table(
    "openers",
    metadata,
    Column("opener_id", Integer, primary_key=True),
    Column("sport", String(8), nullable=False),
    Column("game_id", Integer, nullable=False),
    Column("source", String(32), nullable=False),
    Column("book", String(32), nullable=False),
    Column("market", String(32), nullable=False),
    Column("player_key", String(128), nullable=False),  # normalised player name; "" for games
    Column("player_name", String(128)),
    Column("outcome", String(16), nullable=False),
    Column("line", Float),
    Column("price", Integer),
    Column("first_seen", UTCDateTime, nullable=False),
    UniqueConstraint("source", "book", "game_id", "market", "player_key", "outcome",
                     name="uq_openers"),
)


# M18 Part B: each consensus price with its inputs (MODEL.md "M18 Part B").
consensus = Table(
    "consensus",
    metadata,
    Column("consensus_id", Integer, primary_key=True),
    Column("sport", String(8), nullable=False),
    Column("game_id", Integer, nullable=False),
    Column("market", String(32), nullable=False),
    Column("player_name", String(128)),  # props only
    Column("at", UTCDateTime, nullable=False),
    Column("method", String(16), nullable=False),  # pinnacle, median
    Column("two_book", Boolean, nullable=False),
    Column("n", Integer, nullable=False),
    Column("fair", Float, nullable=False),  # L*, T*, p_home, or the prop over probability
    Column("line", Float),  # props: the line of `fair`
    Column("inputs", JSON, nullable=False),
    Index("ix_consensus_game", "game_id", "market", "at"),
)


# M18 Part B: when each (game, source, book) was last polled and answered. Sweep quotes are
# stored change-only, so a quote is fresh when its book's latest mark is (MODEL.md "M18 Part B").
poll_marks = Table(
    "poll_marks",
    metadata,
    Column("mark_id", Integer, primary_key=True),
    Column("game_id", Integer, nullable=False),
    Column("source", String(32), nullable=False),
    Column("book", String(32), nullable=False),
    Column("polled_at", UTCDateTime, nullable=False),
    Index("ix_poll_marks_game", "game_id", "source", "book", "polled_at"),
)
