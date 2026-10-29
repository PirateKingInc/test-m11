"""The page's props section from stored prop predictions (M15)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import Connection, and_, func, select

from betdata.db.schema import books, games, prop_lines, prop_predictions, teams
from betdata.sports import PropRow


def prop_rows(conn: Connection, now: datetime, model: str, days: int,
              flag_allowed: Callable[[str], bool], research: Callable[[str], bool],
              thresholds: dict[str, float],
              info_only: Callable[[str], bool] | None = None) -> list[PropRow]:  # fmt: skip
    """The latest stored prediction per (game, player, market) known at `now`, for scheduled
    games in the next `days` days, with the line's source and fetch time."""
    pp = prop_predictions
    h, a = teams.alias("h"), teams.alias("a")
    last = (select(pp.c.game_id, pp.c.player_id, pp.c.market,
                   func.max(pp.c.predicted_at).label("at"))
            .join(games, games.c.game_id == pp.c.game_id)
            .where(pp.c.model == model, pp.c.predicted_at <= now, games.c.status == "scheduled",
                   games.c.start_time_utc > now,
                   games.c.start_time_utc <= now + timedelta(days=days))
            .group_by(pp.c.game_id, pp.c.player_id, pp.c.market).subquery())  # fmt: skip
    rows = conn.execute(
        select(pp, games.c.start_time_utc, h.c.abbrev.label("home"), a.c.abbrev.label("away"),
               books.c.key.label("book"))
        .join(last, and_(last.c.game_id == pp.c.game_id, last.c.player_id == pp.c.player_id,
                         last.c.market == pp.c.market, last.c.at == pp.c.predicted_at))
        .join(games, games.c.game_id == pp.c.game_id)
        .join(h, h.c.team_id == games.c.home_team_id)
        .join(a, a.c.team_id == games.c.away_team_id)
        .outerjoin(books, books.c.book_id == pp.c.book_id)
        .where(pp.c.model == model)
        .order_by(games.c.start_time_utc, pp.c.game_id, pp.c.player_name, pp.c.market)
    ).mappings().all()  # fmt: skip
    src = dict(conn.execute(select(prop_lines.c.book_id, prop_lines.c.source).distinct()).all())
    out = []
    for r in rows:
        flag = None
        t = thresholds.get(r["market"])
        if (flag_allowed(r["market"]) and t is not None and r["edge"] is not None
                and abs(r["edge"]) >= t):  # fmt: skip
            flag = "over" if r["edge"] > 0 else "under"
        out.append(PropRow(
            r["game_id"], r["start_time_utc"], r["home"] or "?", r["away"] or "?",
            r["player_name"], r["market"], r["mean"], r["line"], r["over_price"],
            r["under_price"], r["p_over"], r["edge"], list(r["factors"] or []), r["book"],
            src.get(r["book_id"]), r["line_fetched_at"], r["prediction_point"],
            research(r["market"]), flag,
            bool(info_only and info_only(r["market"])),
            (r["dist"] or {}).get("median")))  # fmt: skip
    return out
