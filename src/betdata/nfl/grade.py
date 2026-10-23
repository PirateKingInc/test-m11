"""`betdata grade nfl`: grade stored predictions once games are final.

For every model version, the bet is taken from the last prediction stored before kickoff for
each (game, prediction point): back the side the model prefers wherever it differs from the
market line it was compared against by at least the threshold (the existing thresholds: 3
points on spreads as in M10, 4 on totals as in M11). It is graded at that line at -110 (no
price is stored with the line), and compared with the closing line for closing-line value
(CLV, points better than the close). Postponed or cancelled games are void. Idempotent:
re-running inserts only new grades and updates any whose inputs changed (score corrections).

Moneyline (M13): back the side whose model win probability beats the no-vig market moneyline
stored with the prediction by at least the validated threshold (config/nfl_moneyline.json, in
% points), at that price. A tie pushes. CLV is the side's no-vig probability at the close minus
at the bet, in % points.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, and_, delete, func, insert, select, update

from betdata import moneyline as ml
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
    line: float | None  # the side's own line: home/away spread, or the total; None: moneyline
    edge: float
    price: int = DEFAULT_PRICE


def ml_threshold() -> float:
    from betdata.nfl.moneyline import ML_PATH

    return ml.MoneylineConfig.load(ML_PATH).threshold_pts


def ml_calibration() -> ml.Calibration:
    from betdata.nfl.moneyline import ML_PATH

    return ml.MoneylineConfig.load(ML_PATH).calibration


def moneyline_bet(win_prob: float | None, ml_home: int | None, ml_away: int | None,
                  threshold: float, calib: ml.Calibration | None = None) -> Bet | None:  # fmt: skip
    nv = ml.no_vig(ml_home, ml_away)
    if win_prob is None or nv is None:
        return None
    p = (calib or ml.Calibration())(win_prob)
    edge = 100 * (p - nv[0])
    if abs(edge) < threshold:
        return None
    side = "home" if edge > 0 else "away"
    return Bet("moneyline", side, None, round(abs(edge), 2), ml_home if side == "home" else ml_away)


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
    if bet.market == "moneyline":
        cover = (home - away) if bet.side == "home" else (away - home)
    elif bet.market == "spread":
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


def ml_clv(bet: Bet, bet_prices: tuple[int, int], close: tuple[int, int] | None) -> float | None:
    """No-vig probability of the backed side at the close minus at the bet, % points."""
    if close is None:
        return None
    i = 0 if bet.side == "home" else 1
    return ml.clv_pts(ml.no_vig(*bet_prices)[i], ml.no_vig(*close)[i])


def _ml_closes(conn: Connection, game_ids: list[int]) -> dict[int, tuple[int, int]]:
    """game -> (home, away) closing moneyline from one source; nflverse's own close preferred."""
    by: dict[tuple[int, str], dict[str, int]] = defaultdict(dict)
    for r in conn.execute(
        select(v_closing_lines.c.game_id, v_closing_lines.c.source,
               v_closing_lines.c.game_outcome, v_closing_lines.c.price_american)
        .join(markets, markets.c.market_id == v_closing_lines.c.market_id)
        .where(v_closing_lines.c.game_id.in_(game_ids), markets.c.period == "game",
               markets.c.key == "h2h")
    ):  # fmt: skip
        if r.price_american is not None:
            by[(r.game_id, r.source)][r.game_outcome] = int(r.price_american)
    out: dict[int, tuple[int, tuple[int, int]]] = {}
    for (game, source), d in by.items():
        if "home" in d and "away" in d:
            rank = 0 if source == "nflverse" else 1
            if game not in out or rank < out[game][0]:
                out[game] = (rank, (d["home"], d["away"]))
    return {g: v[1] for g, v in out.items()}


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
    ml_closes = _ml_closes(conn, ids)
    threshold, calib = ml_threshold(), ml_calibration()
    existing = {
        (r.prediction_id, r.market): r
        for r in conn.execute(select(nfl_bet_grades).where(nfl_bet_grades.c.game_id.in_(ids)))
    }
    produced: set[tuple[int, str]] = set()
    for p in preds:
        bets = bets_for(p["pred_home_margin"], p["pred_total"], p["market_spread_home"],
                        p["market_total"])  # fmt: skip
        mlb = moneyline_bet(p["pred_home_win_prob"], p["market_ml_home"], p["market_ml_away"],
                            threshold, calib)  # fmt: skip
        if mlb is not None:
            bets.append(mlb)
        for bet in bets:
            void = p["status"] in VOID_STATUSES
            if not void and p["game_id"] not in finals:
                run.pending += 1
                continue
            result = "void" if void else result_of(bet, *finals[p["game_id"]])
            if bet.market == "moneyline":
                mc = ml_closes.get(p["game_id"])
                close = None
                close_price = None if mc is None else mc[0 if bet.side == "home" else 1]
                bet_clv = ml_clv(bet, (p["market_ml_home"], p["market_ml_away"]), mc)
                won = ml.profit(result, bet.price)
            else:
                close, close_price = closes.get((p["game_id"], bet.side)), None
                bet_clv = clv(bet, close)
                won = profit(result, DEFAULT_PRICE)
            values = dict(
                prediction_id=p["prediction_id"], model_version=p["model_version"],
                game_id=p["game_id"], market=bet.market, side=bet.side, line=bet.line,
                price_american=bet.price, edge=round(bet.edge, 2), close_line=close,
                close_price_american=close_price, clv=None if void else bet_clv,
                result=result, profit=0.0 if void else round(won, 4),
            )  # fmt: skip
            produced.add((p["prediction_id"], bet.market))
            old = existing.get((p["prediction_id"], bet.market))
            if old is None:
                conn.execute(insert(nfl_bet_grades).values(**values, graded_at=now))
                run.inserted += 1
            elif any(getattr(old, k) != v for k, v in values.items()):
                conn.execute(update(nfl_bet_grades)
                             .where(nfl_bet_grades.c.grade_id == old.grade_id)
                             .values(**values, graded_at=now))  # fmt: skip
                run.updated += 1
    # A stored grade whose pick no longer qualifies (a re-grade under corrected rules) goes.
    for key, old in existing.items():
        if key not in produced and old.game_id in finals:
            conn.execute(delete(nfl_bet_grades).where(nfl_bet_grades.c.grade_id == old.grade_id))
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
    # Share of picks that got a better number than the close, among picks whose price or
    # line differed from the close (M15.1: picks taken at the closing price are ties, not
    # misses; with hourly polling most pretip picks are taken at the close).
    beat_close: float | None
    ties: int = 0  # picks whose CLV is exactly 0

    @property
    def bets(self) -> int:
        return self.wins + self.losses + self.pushes

    @property
    def roi(self) -> float | None:
        return self.profit / self.bets if self.bets else None


# What the season record counts (M15.1), one rule for every market: one pick per game, market
# and model version, the last prediction stored before the game started, whenever its edge
# cleared the market's validated threshold, whether or not the card showed a flag. Earlier
# prediction points of the same game are graded and stored but not counted again.
RECORD_RULE = ("One pick per game and market: the model's last prediction before the start, "
               "counted whenever its edge cleared the threshold below (the flag rule), "
               "whether or not a flag was shown.")  # fmt: skip


def records(conn: Connection, table=None) -> list[VersionRecord]:
    table = nfl_bet_grades if table is None else table
    mp = model_predictions
    last: dict[tuple[str, str, int], object] = {}
    for r in conn.execute(select(table, mp.c.predicted_at)
                          .join(mp, mp.c.prediction_id == table.c.prediction_id)):  # fmt: skip
        k = (r.model_version, r.market, r.game_id)
        if k not in last or r.predicted_at > last[k].predicted_at:
            last[k] = r
    acc: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"win": 0, "loss": 0, "push": 0, "void": 0, "profit": 0.0, "clv": []}
    )
    for (mv, market, _), r in last.items():
        a = acc[(mv, market)]
        a[r.result] += 1
        a["profit"] += r.profit
        if r.clv is not None and r.result != "void":
            a["clv"].append(r.clv)
    out = []
    for (mv, market), a in sorted(acc.items()):
        c = a["clv"]
        moved = [x for x in c if x != 0]
        out.append(VersionRecord(mv, market, a["win"], a["loss"], a["push"], a["void"],
                                 round(a["profit"], 4), sum(c) / len(c) if c else None,
                                 sum(x > 0 for x in moved) / len(moved) if moved else None,
                                 len(c) - len(moved)))  # fmt: skip
    return out
