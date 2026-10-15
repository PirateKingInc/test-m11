"""Source-neutral records produced by each source's `parse` step."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ParsedEvent:
    source_game_id: str
    start_time_utc: datetime
    home_name: str
    away_name: str
    neutral_site: bool | None = None
    # Schedule detail, filled by sources that have it.
    season: int | None = None
    season_type: str | None = None  # REG, POST, PRE
    week: int | None = None
    # Final or in-progress score, when the source carries results.
    home_score: int | None = None
    away_score: int | None = None
    final: bool = False


@dataclass(frozen=True)
class ParsedPrice:
    source_game_id: str
    book_key: str
    book_title: str | None
    market_key: str
    outcome: str  # home, away, draw, over, under
    line: float | None
    price_american: int | None
    price_decimal: float | None
    # active or suspended (as the source reports it); "removed" is set by the loader
    price_status: str = "active"
    book_last_update: datetime | None = None
    period: str = "game"
    snapshot_kind: str = "poll"  # poll, source_opening, source_closing


@dataclass
class ParsedOdds:
    sport: str
    fetched_at: datetime
    events: list[ParsedEvent] = field(default_factory=list)
    prices: list[ParsedPrice] = field(default_factory=list)
    # Markets that were requested. A selection we saw before in one of these markets, for an
    # event present in this response, that is now missing gets a "removed" row.
    requested_markets: tuple[str, ...] = ()
    # Books requested explicitly; None = every book the source returns for the region.
    requested_books: tuple[str, ...] | None = None
    # True when this source owns game schedule fields and home/away orientation.
    schedule_authority: bool = False
