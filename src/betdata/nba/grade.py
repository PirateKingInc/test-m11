"""Grade stored NBA predictions once games are final (the NFL grader's rules, M12/M13):

For every model version, the bet is taken from the last prediction stored before tip for each
(game, prediction point): the side the model prefers where it differs from the market line it
was compared against by at least the validated threshold (config/nba_model.json), graded at
that line at -110; moneyline at the stored price when the model's win probability beats the
no-vig market by the moneyline threshold. CLV against the closing line (`v_closing_lines`: a
source's own close, else its last poll before tip). Postponed or cancelled games are void.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import Connection, and_, func, insert, select, update

from betdata import moneyline as ml
from betdata.analysis.backtest import DEFAULT_PRICE, profit
from betdata.core.matching import sport_id
from betdata.db.schema import games, model_predictions, nba_bet_grades, results
from betdata.nfl.grade import (
    VOID_STATUSES,
    Bet,
    GradeRun,
    VersionRecord,
    _closes,
    _ml_closes,
    clv,
    ml_clv,
    moneyline_bet,
    result_of,
)


def latest_predictions(conn: Connection) -> list:
    sid = sport_id(conn, "nba")
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
        select(mp, games.c.status)
        .join(games, games.c.game_id == mp.c.game_id)
        .join(last, and_(last.c.model_version == mp.c.model_version,
                         last.c.game_id == mp.c.game_id,
                         last.c.pt == func.coalesce(mp.c.prediction_point, ""),
                         last.c.at == mp.c.predicted_at))
    ).mappings().all()  # fmt: skip


def spread_bet(pred_margin, spread_home, threshold) -> Bet | None:
    if pred_margin is None or spread_home is None:
        return None
    edge = pred_margin + spread_home
    if abs(edge) < threshold:
        return None
    side = "home" if edge > 0 else "away"
    return Bet("spread", side, spread_home if side == "home" else -spread_home, abs(edge))


def grade_nba(conn: Connection, now: datetime, spread_threshold: float,
              ml_threshold: float) -> GradeRun:  # fmt: skip
    run = GradeRun()
    preds = latest_predictions(conn)
    ids = sorted({p["game_id"] for p in preds})
    finals = {r.game_id: (r.home_score, r.away_score) for r in conn.execute(
        select(results.c.game_id, results.c.home_score, results.c.away_score)
        .where(results.c.game_id.in_(ids), results.c.final.is_(True)))}  # fmt: skip
    closes, ml_closes = _closes(conn, ids), _ml_closes(conn, ids)
    existing = {(r.prediction_id, r.market): r for r in conn.execute(
        select(nba_bet_grades).where(nba_bet_grades.c.game_id.in_(ids)))}  # fmt: skip
    for p in preds:
        bets = [b for b in (spread_bet(p["pred_home_margin"], p["market_spread_home"],
                                       spread_threshold),
                            moneyline_bet(p["pred_home_win_prob"], p["market_ml_home"],
                                          p["market_ml_away"], ml_threshold)) if b]  # fmt: skip
        for bet in bets:
            void = p["status"] in VOID_STATUSES
            if not void and p["game_id"] not in finals:
                run.pending += 1
                continue
            result = "void" if void else result_of(bet, *finals[p["game_id"]])
            if bet.market == "moneyline":
                mc = ml_closes.get(p["game_id"])
                close, close_price = None, None if mc is None else mc[bet.side != "home"]
                bet_clv = ml_clv(bet, (p["market_ml_home"], p["market_ml_away"]), mc)
                won = ml.profit(result, bet.price)
            else:
                close, close_price = closes.get((p["game_id"], bet.side)), None
                bet_clv = clv(bet, close)
                won = profit(result, DEFAULT_PRICE)
            values = dict(prediction_id=p["prediction_id"], model_version=p["model_version"],
                          game_id=p["game_id"], market=bet.market, side=bet.side,
                          line=bet.line, price_american=bet.price, edge=round(bet.edge, 2),
                          close_line=close, close_price_american=close_price,
                          clv=None if void else bet_clv, result=result,
                          profit=0.0 if void else round(won, 4))  # fmt: skip
            old = existing.get((p["prediction_id"], bet.market))
            if old is None:
                conn.execute(insert(nba_bet_grades).values(**values, graded_at=now))
                run.inserted += 1
            elif any(getattr(old, k) != v for k, v in values.items()):
                conn.execute(update(nba_bet_grades)
                             .where(nba_bet_grades.c.grade_id == old.grade_id)
                             .values(**values, graded_at=now))  # fmt: skip
                run.updated += 1
    return run


def records(conn: Connection) -> list[VersionRecord]:
    acc: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"win": 0, "loss": 0, "push": 0, "void": 0, "profit": 0.0, "clv": []})
    for r in conn.execute(select(nba_bet_grades)):
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
