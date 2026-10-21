"""NBA prop grading against the box score (`betdata grade nba --props`)."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Connection, select

from betdata.config import PROJECT_ROOT
from betdata.db.schema import nba_player_games
from betdata.nba.props.panel import MARKETS
from betdata.props.grade import VOID, GradeRun, grade_props

MODEL = "nbap15"
DESIGN_PATH = PROJECT_ROOT / "config" / "nba_props.json"


def thresholds() -> dict[str, float]:
    """Validated edge thresholds (% points) of the markets that have a backtest."""
    if not DESIGN_PATH.exists():
        return {}
    d = json.loads(DESIGN_PATH.read_text())
    return {m: float(t) for m, t in (d.get("thresholds") or {}).items() if t is not None}


def actuals(conn: Connection, keys: list[tuple[int, str, str]]) -> dict:
    t = nba_player_games
    gids = sorted({k[0] for k in keys})
    rows = conn.execute(select(t.c.game_id, t.c.player_id, t.c.minutes, t.c.points, t.c.oreb,
                               t.c.dreb, t.c.ast, t.c.fg3m)
                        .where(t.c.game_id.in_(gids))).all()  # fmt: skip
    have_box = {r.game_id for r in rows}
    line = {(r.game_id, r.player_id): r for r in rows}
    out = {}
    for gid, pid, market in keys:
        if gid not in have_box:
            continue  # box score not in yet
        r = line.get((gid, pid))
        if r is None or not (r.minutes or 0) > 0:
            out[(gid, pid, market)] = VOID
            continue
        reb = (r.oreb or 0) + (r.dreb or 0)
        val = {"pts": r.points or 0, "reb": reb, "ast": r.ast or 0, "fg3m": r.fg3m or 0,
               "pra": (r.points or 0) + reb + (r.ast or 0)}[MARKETS[market]]  # fmt: skip
        out[(gid, pid, market)] = float(val)
    return out


def grade_nba_props(conn: Connection, now: datetime) -> GradeRun:
    return grade_props(conn, now, (MODEL,), actuals, thresholds())
