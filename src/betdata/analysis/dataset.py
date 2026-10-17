"""One row per game for modelling: teams, kickoff, final score, closing spread and total.

Built only from what the ingestion layer already stores (games, results, v_closing_lines), so
any source with closing lines can feed a backtest. NFL history comes from nflverse.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, column, select, table

from betdata.core.matching import sport_id
from betdata.db.schema import games, markets, results, teams

# The view is created by migration 0005; declared here only to query it.
v_closing_lines = table(
    "v_closing_lines",
    column("game_id"),
    column("source"),
    column("book_id"),
    column("market_id"),
    column("game_outcome"),
    column("line"),
    column("price_american"),
)


@dataclass(frozen=True)
class GameRow:
    game_id: int
    season: int
    season_type: str | None
    week: int | None
    start: datetime
    home_team_id: int
    away_team_id: int
    home: str  # abbreviation, for reports
    away: str
    neutral: bool
    home_score: int | None
    away_score: int | None
    # Closing line on the home side: -3.0 means the home team is favoured by 3.
    spread_home: float | None
    spread_home_price: int | None
    spread_away_price: int | None
    total: float | None
    over_price: int | None
    under_price: int | None
    # Days since the team's previous game this season; None for its first game.
    home_rest_days: float | None
    away_rest_days: float | None

    @property
    def final(self) -> bool:
        return self.home_score is not None and self.away_score is not None

    @property
    def home_margin(self) -> int | None:
        return self.home_score - self.away_score if self.final else None


def _days_since(prev: datetime | None, now: datetime) -> float | None:
    return None if prev is None else round((now - prev).total_seconds() / 86400, 1)


def load_games(
    conn: Connection, sport: str = "nfl", *, lines_source: str = "nflverse"
) -> list[GameRow]:
    """Every game of a sport in kickoff order, with closing lines from `lines_source`.

    Scores come from the lines source when it has them, else from any source with a final
    result. A game with no line still appears (the model rates it; nothing can be bet on it).
    """
    sid = sport_id(conn, sport)
    names = {
        r.team_id: r.abbrev or r.canonical_name
        for r in conn.execute(select(teams.c.team_id, teams.c.abbrev, teams.c.canonical_name))
    }

    scores: dict[int, tuple[int, int]] = {}
    for r in conn.execute(
        select(results.c.game_id, results.c.source, results.c.home_score, results.c.away_score)
        .join(games, games.c.game_id == results.c.game_id)
        .where(games.c.sport_id == sid, results.c.final.is_(True))
        .order_by(results.c.game_id, results.c.source)
    ):
        if r.home_score is None or r.away_score is None:
            continue
        if r.game_id not in scores or r.source == lines_source:
            scores[r.game_id] = (r.home_score, r.away_score)

    lines: dict[int, dict[tuple[str, str], tuple[float | None, int | None]]] = defaultdict(dict)
    for r in conn.execute(
        select(
            v_closing_lines.c.game_id,
            markets.c.key,
            v_closing_lines.c.game_outcome,
            v_closing_lines.c.line,
            v_closing_lines.c.price_american,
        )  # fmt: skip
        .join(markets, markets.c.market_id == v_closing_lines.c.market_id)
        .join(games, games.c.game_id == v_closing_lines.c.game_id)
        .where(
            games.c.sport_id == sid,
            v_closing_lines.c.source == lines_source,
            markets.c.period == "game",
            markets.c.key.in_(("spreads", "totals")),
        )
        # One book per source in practice (nflverse); with several, the lowest book_id wins.
        .order_by(v_closing_lines.c.book_id.desc())
    ):
        lines[r.game_id][(r.key, r.game_outcome)] = (r.line, r.price_american)

    out: list[GameRow] = []
    last_played: dict[tuple[int, int], datetime] = {}  # (season, team) -> previous kickoff
    for g in conn.execute(
        select(games)
        .where(games.c.sport_id == sid)
        .order_by(games.c.start_time_utc, games.c.game_id)
    ):
        home_rest, away_rest = (
            _days_since(last_played.get((g.season, t)), g.start_time_utc)
            for t in (g.home_team_id, g.away_team_id)
        )
        last_played[(g.season, g.home_team_id)] = g.start_time_utc
        last_played[(g.season, g.away_team_id)] = g.start_time_utc

        gl = lines.get(g.game_id, {})
        spread_line, home_price = gl.get(("spreads", "home"), (None, None))
        away_price = gl.get(("spreads", "away"), (None, None))[1]
        total_line, over_price = gl.get(("totals", "over"), (None, None))
        under_price = gl.get(("totals", "under"), (None, None))[1]
        home_score, away_score = scores.get(g.game_id, (None, None))
        out.append(
            GameRow(
                game_id=g.game_id,
                season=g.season,
                season_type=g.season_type,
                week=g.week,
                start=g.start_time_utc,
                home_team_id=g.home_team_id,
                away_team_id=g.away_team_id,
                home=names.get(g.home_team_id, str(g.home_team_id)),
                away=names.get(g.away_team_id, str(g.away_team_id)),
                neutral=bool(g.neutral_site),
                home_score=home_score,
                away_score=away_score,
                spread_home=spread_line,
                spread_home_price=home_price,
                spread_away_price=away_price,
                total=total_line,
                over_price=over_price,
                under_price=under_price,
                home_rest_days=home_rest,
                away_rest_days=away_rest,
            )
        )
    return out
