"""`betdata grade nfl`: grade stored predictions once games are final.

For every model version, the bet is taken from the last prediction stored before kickoff for
each (game, prediction point): back the side the model prefers wherever it differs from the
market line it was compared against by at least the threshold (the existing thresholds: 3
points on spreads as in M10, 4 on totals as in M11). It is graded at that line at -110 (no
price is stored with the line), and compared with the closing line for closing-line value
(CLV, points better than the close). Postponed or cancelled games are void. Idempotent:
re-running inserts only new grades and updates any whose inputs changed (score corrections).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, and_, func, insert, select, update

from betdata.analysis.backtest import DEFAULT_PRICE, profit
from betdata.analysis.dataset import v_closing_lines
from betdata.core.matching import sport_id
from betdata.db.schema import games, markets, model_predictions, nfl_bet_grades, results
from betdata.nfl.selection import BET_THRESHOLDS

THRESHOLDS = BET_THRESHOLDS
VOID_STATUSES = ("postponed", "cancelled")


@dataclass(frozen=True)
class Bet:
    market: str
    side: str
    line: float  # the side's own line: home/away spread, or the total
    edge: float


def bets_for(pred_margin: float | None, pred_total: float | None, spread_home: float | None,
             total: float | None) -> list[Bet]:  # fmt: skip
    out = []
    if pred_margin is not None and spread_home is not None:
        edge = pred_margin + spread_home  # the market expects the home team to win by -spread
        if abs(edge) >= THRESHOLDS["spread"]:
            side = "home" if edge > 0 else "away"
            out.append(Bet("spread", side, spread_home if side == "home" else -spread_home,
                           abs(edge)))  # fmt: skip
    if pred_total is not None and total is not None:
        edge = pred_total - total
        if abs(edge) >= THRESHOLDS["total"]:
            out.append(Bet("total", "over" if edge > 0 else "under", total, abs(edge)))
    return out


def result_of(bet: Bet, home: int, away: int) -> str:
    if bet.market == "spread":
        margin = home - away if bet.side == "home" else away - home
        cover = margin + bet.line
    else:
        pts = home + away
        cover = pts - bet.line if bet.side == "over" else bet.line - pts
    return "win" if cover > 0 else "loss" if cover < 0 else "push"


def clv(bet: Bet, close_line: float | None) -> float | None:
    """Points better than the close: more points on a spread side, a lower over / higher
    under on a total."""
    if close_line is None:
        return None
    if bet.market == "spread":
        return round(bet.line - close_line, 2)
    return round(close_line - bet.line if bet.side == "over" else bet.line - close_line, 2)


def _closes(conn: Connection, game_ids: list[int]) -> dict[tuple[int, str], float]:
    """(game, side) -> closing line of that side; nflverse's own close preferred."""
    out: dict[tuple[int, str], tuple[int, float]] = {}
    rows = conn.execute(
        select(v_closing_lines.c.game_id, v_closing_lines.c.source, v_closing_lines.c.line,
               v_closing_lines.c.game_outcome, markets.c.key)
        .join(markets, markets.c.market_id == v_closing_lines.c.market_id)
        .where(v_closing_lines.c.game_id.in_(game_ids), markets.c.period == "game",
               markets.c.key.in_(("spreads", "totals")))
    ).all()  # fmt: skip
    for r in rows:
        if r.line is None:
            continue
        rank = 0 if r.source == "nflverse" else 1
        side = r.game_outcome  # home, away, over, under
        if (r.game_id, side) not in out or rank < out[(r.game_id, side)][0]:
            out[(r.game_id, side)] = (rank, float(r.line))
    return {k: v[1] for k, v in out.items()}


def latest_predictions(conn: Connection) -> list:
    """The last row stored before kickoff per (model version, game, prediction point)."""
    sid = sport_id(conn, "nfl")
    mp = model_predictions
    point = func.coalesce(mp.c.prediction_point, "")
    last = (
        select(mp.c.model_version, mp.c.game_id, point.label("pt"),
               func.max(mp.c.predicted_at).label("at"))
        .join(games, games.c.game_id == mp.c.game_id)
        .where(games.c.sport_id == sid, mp.c.predicted_at < games.c.start_time_utc)
        .group_by(mp.c.model_version, mp.c.game_id, point)
        .subquery()
    )  # fmt: skip
    return conn.execute(
        select(mp, games.c.status, games.c.start_time_utc)
        .join(games, games.c.game_id == mp.c.game_id)
        .join(last, and_(last.c.model_version == mp.c.model_version,
                         last.c.game_id == mp.c.game_id,
                         last.c.pt == func.coalesce(mp.c.prediction_point, ""),
                         last.c.at == mp.c.predicted_at))
    ).mappings().all()  # fmt: skip


@dataclass
class GradeRun:
    inserted: int = 0
    updated: int = 0
    pending: int = 0  # bets on games not final yet


def grade_nfl(conn: Connection, now: datetime) -> GradeRun:
    run = GradeRun()
    preds = latest_predictions(conn)
    ids = sorted({p["game_id"] for p in preds})
    finals = {
        r.game_id: (r.home_score, r.away_score)
        for r in conn.execute(
            select(results.c.game_id, results.c.home_score, results.c.away_score,
                   results.c.source)
            .where(results.c.game_id.in_(ids), results.c.final.is_(True))
            .order_by(results.c.source.desc())  # nflverse last, so it wins
        )
    }  # fmt: skip
    closes = _closes(conn, ids)
    existing = {
        (r.prediction_id, r.market): r
        for r in conn.execute(select(nfl_bet_grades).where(nfl_bet_grades.c.game_id.in_(ids)))
    }
    for p in preds:
        bets = bets_for(p["pred_home_margin"], p["pred_total"], p["market_spread_home"],
                        p["market_total"])  # fmt: skip
        for bet in bets:
            void = p["status"] in VOID_STATUSES
            if not void and p["game_id"] not in finals:
                run.pending += 1
                continue
            result = "void" if void else result_of(bet, *finals[p["game_id"]])
            close = closes.get((p["game_id"], bet.side))
            values = dict(
                prediction_id=p["prediction_id"], model_version=p["model_version"],
                game_id=p["game_id"], market=bet.market, side=bet.side, line=bet.line,
                price_american=DEFAULT_PRICE, edge=round(bet.edge, 2), close_line=close,
                clv=None if void else clv(bet, close), result=result,
                profit=0.0 if void else round(profit(result, DEFAULT_PRICE), 4),
            )  # fmt: skip
            old = existing.get((p["prediction_id"], bet.market))
            if old is None:
                conn.execute(insert(nfl_bet_grades).values(**values, graded_at=now))
                run.inserted += 1
            elif any(getattr(old, k) != v for k, v in values.items()):
                conn.execute(update(nfl_bet_grades)
                             .where(nfl_bet_grades.c.grade_id == old.grade_id)
                             .values(**values, graded_at=now))  # fmt: skip
                run.updated += 1
    return run


@dataclass
class VersionRecord:
    model_version: str
    market: str
    wins: int
    losses: int
    pushes: int
    voids: int
    profit: float
    avg_clv: float | None
    beat_close: float | None  # share of bets with a close that got a better number

    @property
    def bets(self) -> int:
        return self.wins + self.losses + self.pushes

    @property
    def roi(self) -> float | None:
        return self.profit / self.bets if self.bets else None


def records(conn: Connection) -> list[VersionRecord]:
    acc: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"win": 0, "loss": 0, "push": 0, "void": 0, "profit": 0.0, "clv": []}
    )
    for r in conn.execute(select(nfl_bet_grades)):
        a = acc[(r.model_version, r.market)]
        a[r.result] += 1
        a["profit"] += r.profit
        if r.clv is not None and r.result != "void":
            a["clv"].append(r.clv)
    out = []
    for (mv, market), a in sorted(acc.items()):
        c = a["clv"]
        out.append(VersionRecord(mv, market, a["win"], a["loss"], a["push"], a["void"],
                                 round(a["profit"], 4), sum(c) / len(c) if c else None,
                                 sum(x > 0 for x in c) / len(c) if c else None))  # fmt: skip
    return out
