"""Parsers for CFBD/CBBD `/games` and `/lines` (pure functions; no I/O).

Field names follow the official generated clients: cfbd 5.x `Game`, `BettingGame`, `GameLine`
and cbbd 1.x `GameInfo`, `GameLines`, `GameLineInfo`.

Lines: one row per provider (consensus, DraftKings, ESPN Bet, ...), each with a current spread,
total and moneylines plus the opening spread and total. Stored as
  * `source_opening` for spreadOpen / overUnderOpen,
  * `poll` for the current numbers while the game is upcoming (change-only, so line movement
    between daily pulls is kept), and `source_closing` once the game is final.
Prices are not given for spreads/totals, so those rows have a line and no price.

Spread direction: football has `formattedSpread` ("Georgia -21.5"), which names the favorite;
the home line is derived from it, and the raw `spread` is only a fallback. Basketball has no
formatted spread, so `spread` is taken as the home team's line and checked against the
moneylines; disagreements are logged.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from pydantic import BaseModel, TypeAdapter, ValidationError

from betdata.core.errors import ParseError
from betdata.core.matching import season_for
from betdata.core.models import ParsedEvent, ParsedOdds, ParsedPrice
from betdata.core.normalize.odds_math import american_to_decimal

log = logging.getLogger(__name__)
FOOTBALL_LEVELS = {"fbs", "fcs"}
SEASON_TYPES = {"regular": "REG", "postseason": "POST", "preseason": "PRE"}
# A response of exactly one of these sizes may have been cut off by a server-side cap.
SUSPICIOUS_SIZES = {1000, 2000, 2500, 3000, 5000, 10000}


def game_id(sport: str, cfbd_id: int) -> str:
    """Football and basketball ids come from different databases; keep them apart."""
    return f"{sport}:{cfbd_id}"


class _Line(BaseModel):
    provider: str
    spread: float | None = None
    formattedSpread: str | None = None
    spreadOpen: float | None = None
    overUnder: float | None = None
    overUnderOpen: float | None = None
    homeMoneyline: float | None = None
    awayMoneyline: float | None = None


class _FootballGame(BaseModel):
    id: int
    season: int
    week: int | None = None
    seasonType: str
    startDate: datetime
    completed: bool = False
    neutralSite: bool = False
    venueId: int | None = None
    venue: str | None = None
    homeId: int
    homeTeam: str
    homeClassification: str | None = None
    homePoints: int | None = None
    awayId: int
    awayTeam: str
    awayClassification: str | None = None
    awayPoints: int | None = None


class _FootballLines(BaseModel):
    id: int
    season: int
    seasonType: str
    week: int | None = None
    startDate: datetime
    homeTeamId: int
    homeTeam: str
    homeClassification: str | None = None
    homeScore: int | None = None
    awayTeamId: int
    awayTeam: str
    awayClassification: str | None = None
    awayScore: int | None = None
    lines: list[_Line] = []


class _BasketballGame(BaseModel):
    id: int
    sourceId: str | None = None  # ESPN game id
    seasonType: str | None = None
    startDate: datetime
    neutralSite: bool = False
    status: str
    homeTeamId: int
    homeTeam: str
    homePoints: int | None = None
    awayTeamId: int
    awayTeam: str
    awayPoints: int | None = None


class _BasketballLines(BaseModel):
    gameId: int
    seasonType: str | None = None
    startDate: datetime
    homeTeamId: int
    homeTeam: str
    homeScore: float | None = None
    awayTeamId: int
    awayTeam: str
    awayScore: float | None = None
    lines: list[_Line] = []


def _validate(model, body: bytes, what: str):
    try:
        items = TypeAdapter(list[model]).validate_json(body)
    except ValidationError as e:
        raise ParseError(f"cfbd: unexpected {what} response: {e}") from e
    if len(items) in SUSPICIOUS_SIZES:
        log.warning("cfbd %s returned exactly %d rows; it may be truncated", what, len(items))
    return items


def _both_levels_ok(home: str | None, away: str | None) -> bool:
    return (home or "").lower() in FOOTBALL_LEVELS and (away or "").lower() in FOOTBALL_LEVELS


# --- games ----------------------------------------------------------------------


def parse_football_games(body: bytes, fetched_at: datetime) -> ParsedOdds:
    out = ParsedOdds(sport="ncaaf", fetched_at=fetched_at, schedule_authority=True)
    for g in _validate(_FootballGame, body, "football /games"):
        if not _both_levels_ok(g.homeClassification, g.awayClassification):
            continue  # D-II/III opponents: not on betting boards, not in our team list
        final = g.completed and g.homePoints is not None and g.awayPoints is not None
        out.events.append(
            ParsedEvent(
                source_game_id=game_id("ncaaf", g.id),
                start_time_utc=g.startDate,
                home_name=g.homeTeam,
                away_name=g.awayTeam,
                neutral_site=g.neutralSite,
                season=g.season,
                season_type=SEASON_TYPES.get(g.seasonType, g.seasonType),
                week=g.week,
                home_score=g.homePoints if final else None,
                away_score=g.awayPoints if final else None,
                final=final,
                status="final" if final else "scheduled",
                home_source_team_id=str(g.homeId),
                away_source_team_id=str(g.awayId),
                venue_source_id=str(g.venueId) if g.venueId is not None else None,
                venue_name=g.venue,
            )
        )
    return out


def parse_basketball_games(body: bytes, fetched_at: datetime) -> ParsedOdds:
    out = ParsedOdds(sport="ncaab", fetched_at=fetched_at, schedule_authority=True)
    for g in _validate(_BasketballGame, body, "basketball /games"):
        final = g.status == "final" and g.homePoints is not None and g.awayPoints is not None
        out.events.append(
            ParsedEvent(
                source_game_id=game_id("ncaab", g.id),
                start_time_utc=g.startDate,
                home_name=g.homeTeam,
                away_name=g.awayTeam,
                neutral_site=g.neutralSite,
                season=season_for("ncaab", g.startDate),
                season_type=SEASON_TYPES.get(g.seasonType or "", g.seasonType),
                home_score=g.homePoints if final else None,
                away_score=g.awayPoints if final else None,
                final=final,
                status=g.status,
                home_source_team_id=str(g.homeTeamId),
                away_source_team_id=str(g.awayTeamId),
                extra_ids={"espn": g.sourceId} if g.sourceId else {},
            )
        )
    return out


# --- lines ----------------------------------------------------------------------

_FORMATTED = re.compile(r"^(?P<team>.+?)\s+(?P<num>[-+]?\d+(?:\.\d+)?)$")


def _formatted_home_spread(formatted: str | None, home: str, away: str) -> float | None:
    """'Georgia -21.5' with Georgia at home -> -21.5; with Georgia away -> +21.5."""
    if not formatted:
        return None
    text = formatted.strip()
    if text.lower() in ("pk", "pick", "pick'em", "even"):
        return 0.0
    m = _FORMATTED.match(text)
    if not m:
        return None
    team, num = m.group("team").strip(), float(m.group("num"))
    if team == home:
        return num
    if team == away:
        return -num
    return None


def _home_spread_sign(line: _Line, home: str, away: str) -> float:
    """+1 if `spread` is already the home line, -1 if the formatted spread says it's inverted."""
    formatted = _formatted_home_spread(line.formattedSpread, home, away)
    if formatted is None or line.spread is None or formatted == line.spread:
        return 1.0
    if formatted == -line.spread:
        return -1.0
    log.warning(
        "cfbd %s: spread %s disagrees with %r", line.provider, line.spread, line.formattedSpread
    )
    return 1.0


def _price(gid, book, market, outcome, line_value, american, kind) -> ParsedPrice:
    american = round(american) if american is not None else None
    return ParsedPrice(
        source_game_id=gid,
        book_key=book,
        book_title=book,
        market_key=market,
        outcome=outcome,
        line=line_value,
        price_american=american,
        price_decimal=american_to_decimal(american) if american else None,
        snapshot_kind=kind,
    )


def _line_prices(gid: str, home: str, away: str, line: _Line, final: bool) -> list[ParsedPrice]:
    book = line.provider
    kind = "source_closing" if final else "poll"
    sign = _home_spread_sign(line, home, away)
    out: list[ParsedPrice] = []

    def spreads(value, k):
        h = value * sign + 0.0  # + 0.0 turns -0.0 into 0.0
        out.extend([_price(gid, book, "spreads", "home", h, None, k),
                    _price(gid, book, "spreads", "away", -h + 0.0, None, k)])  # fmt: skip

    def totals(value, k):
        out.extend([_price(gid, book, "totals", "over", value, None, k),
                    _price(gid, book, "totals", "under", value, None, k)])  # fmt: skip

    if line.spread is not None:
        spreads(line.spread, kind)
    if line.overUnder is not None:
        totals(line.overUnder, kind)
    valid_ml = all(
        m is not None and abs(m) >= 100 for m in (line.homeMoneyline, line.awayMoneyline)
    )
    if valid_ml:
        out.append(_price(gid, book, "h2h", "home", None, line.homeMoneyline, kind))
        out.append(_price(gid, book, "h2h", "away", None, line.awayMoneyline, kind))
        if line.spread is not None and line.spread * sign != 0:
            home_favored_by_spread = line.spread * sign < 0
            home_favored_by_ml = line.homeMoneyline < line.awayMoneyline
            if home_favored_by_spread != home_favored_by_ml:
                log.warning(
                    "cfbd %s %s: spread %s and moneylines %s/%s disagree on the favorite",
                    gid, book, line.spread, line.homeMoneyline, line.awayMoneyline,
                )  # fmt: skip
    if line.spreadOpen is not None:
        spreads(line.spreadOpen, "source_opening")
    if line.overUnderOpen is not None:
        totals(line.overUnderOpen, "source_opening")
    return out


def parse_football_lines(body: bytes, fetched_at: datetime) -> ParsedOdds:
    out = ParsedOdds(sport="ncaaf", fetched_at=fetched_at)
    for g in _validate(_FootballLines, body, "football /lines"):
        if not _both_levels_ok(g.homeClassification, g.awayClassification):
            continue
        gid = game_id("ncaaf", g.id)
        final = g.homeScore is not None and g.awayScore is not None
        out.events.append(
            ParsedEvent(
                gid,
                g.startDate,
                g.homeTeam,
                g.awayTeam,
                home_source_team_id=str(g.homeTeamId),
                away_source_team_id=str(g.awayTeamId),
            )  # fmt: skip
        )
        for line in g.lines:
            out.prices += _line_prices(gid, g.homeTeam, g.awayTeam, line, final)
    return out


def parse_basketball_lines(body: bytes, fetched_at: datetime) -> ParsedOdds:
    out = ParsedOdds(sport="ncaab", fetched_at=fetched_at)
    for g in _validate(_BasketballLines, body, "basketball /lines"):
        gid = game_id("ncaab", g.gameId)
        final = g.homeScore is not None and g.awayScore is not None
        out.events.append(
            ParsedEvent(
                gid,
                g.startDate,
                g.homeTeam,
                g.awayTeam,
                home_source_team_id=str(g.homeTeamId),
                away_source_team_id=str(g.awayTeamId),
            )  # fmt: skip
        )
        for line in g.lines:
            out.prices += _line_prices(gid, g.homeTeam, g.awayTeam, line, final)
    return out
