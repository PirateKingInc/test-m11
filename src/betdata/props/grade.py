"""Grade stored prop predictions once the box score is in (MODEL.md, "M15 grading and page").

Every (model version, game, player, market, prediction point) is graded from its last stored
prediction before tip:
  outcome  over / under / push against the line it was compared with; `void` when the player
           did not play; `no_line` when no line was stored (graded on the stat only).
  bet      only where the market has a validated edge threshold and |edge| cleared it: the
           side the model prefers, at the stored price; profit per 1 unit staked.
  CLV      against the closing line (the last line stored before tip): for an over, close line
           minus bet line (an under the reverse), in the stat's units; at an unchanged line, the
           move in the bet side's no-vig probability is in `clv` as % points instead, flagged by
           `close_line == line`. None when no closing line was stored.
Yes/no markets (anytime touchdown) have no line: `over` means yes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, and_, func, insert, select, update

from betdata import moneyline as ml
from betdata.db.schema import games, prop_grades, prop_lines, prop_predictions

VOID = "void"
# actuals(conn, keys) -> {(game_id, player_id, market): value, or VOID if he did not play};
# keys whose box score is not in yet are left out (graded later).
Actuals = Callable[[Connection, list[tuple[int, str, str]]], dict]


@dataclass
class GradeRun:
    inserted: int = 0
    updated: int = 0
    pending: int = 0


def latest_predictions(conn: Connection, models: tuple[str, ...]) -> list:
    pp = prop_predictions
    last = (select(pp.c.model_version, pp.c.game_id, pp.c.player_id, pp.c.market,
                   pp.c.prediction_point, func.max(pp.c.predicted_at).label("at"))
            .join(games, games.c.game_id == pp.c.game_id)
            .where(pp.c.model.in_(models), pp.c.predicted_at < games.c.start_time_utc,
                   games.c.status == "final")
            .group_by(pp.c.model_version, pp.c.game_id, pp.c.player_id, pp.c.market,
                      pp.c.prediction_point).subquery())  # fmt: skip
    return conn.execute(
        select(pp, games.c.start_time_utc.label("tip"))
        .join(games, games.c.game_id == pp.c.game_id)
        .join(last, and_(*(last.c[k] == pp.c[k] for k in (
            "model_version", "game_id", "player_id", "market", "prediction_point")),
            last.c.at == pp.c.predicted_at))).mappings().all()  # fmt: skip


def closing_lines(conn: Connection, keys: set[tuple[int, str, str]], tips: dict[int, datetime]
                  ) -> dict[tuple[int, str, str], tuple]:  # fmt: skip
    """(game, player, market) -> (line, over_price, under_price): a history `close` row, else
    the last line stored before tip."""
    if not keys:
        return {}
    gids = sorted({k[0] for k in keys})
    rows = conn.execute(select(prop_lines.c.game_id, prop_lines.c.player_id, prop_lines.c.market,
                               prop_lines.c.kind, prop_lines.c.line, prop_lines.c.over_price,
                               prop_lines.c.under_price, prop_lines.c.fetched_at)
                        .where(prop_lines.c.game_id.in_(gids))
                        .order_by(prop_lines.c.fetched_at)).all()  # fmt: skip
    close, poll = {}, {}
    for r in rows:
        k = (r.game_id, r.player_id, r.market)
        if k not in keys:
            continue
        if r.kind == "close":
            close[k] = (r.line, r.over_price, r.under_price)
        elif r.kind == "poll" and r.fetched_at < tips[r.game_id]:
            poll[k] = (r.line, r.over_price, r.under_price)
    return {**poll, **close}


def outcome(actual: float, line: float | None, yes_no: bool) -> str:
    if line is None:
        return ("over" if actual >= 1 else "under") if yes_no else "no_line"
    return "over" if actual > line else "under" if actual < line else "push"


def bet_side(edge: float | None, threshold: float | None) -> str | None:
    if edge is None or threshold is None or abs(edge) < threshold:
        return None
    return "over" if edge > 0 else "under"


def clv(side: str, line, over_price, under_price, close) -> float | None:
    if close is None or close[0] is None and line is not None:
        return None
    c_line, c_over, c_under = close
    if line is not None and c_line is not None and c_line != line:
        return (c_line - line) if side == "over" else (line - c_line)
    bet = ml.no_vig(over_price, under_price)
    cl = ml.no_vig(c_over, c_under)
    if not bet or not cl:
        return None
    i = 0 if side == "over" else 1
    return round(100 * (cl[i] - bet[i]), 3)


def grade_props(conn: Connection, now: datetime, models: tuple[str, ...], actuals: Actuals,
                thresholds: dict[str, float], yes_no: tuple[str, ...] = ("anytime_td",),
                ) -> GradeRun:  # fmt: skip
    run = GradeRun()
    preds = latest_predictions(conn, models)
    keys = sorted({(p["game_id"], p["player_id"], p["market"]) for p in preds})
    got = actuals(conn, keys)
    tips = {p["game_id"]: p["tip"] for p in preds}
    closes = closing_lines(conn, set(keys), tips)
    have = {r.prop_prediction_id: r for r in conn.execute(
        select(prop_grades).where(prop_grades.c.game_id.in_(sorted(tips))))}  # fmt: skip
    for p in preds:
        k = (p["game_id"], p["player_id"], p["market"])
        if k not in got:
            run.pending += 1
            continue
        a = got[k]
        side = bet_side(p["edge"], thresholds.get(p["market"]))
        close = closes.get(k)
        vals = dict(prop_prediction_id=p["prop_prediction_id"], model_version=p["model_version"],
                    game_id=p["game_id"], player_id=p["player_id"], market=p["market"],
                    close_line=close[0] if close else None)  # fmt: skip
        if a == VOID:
            vals.update(actual=None, outcome=VOID, bet_side=side, price_american=None,
                        profit=0.0 if side else None, close_price_american=None, clv=None)
        else:
            res = outcome(float(a), p["line"], p["market"] in yes_no)
            price = (p["over_price"] if side == "over" else p["under_price"]) if side else None
            prof = None
            if side and price is not None:
                won = "push" if res == "push" else "win" if res == side else "loss"
                prof = round(ml.profit(won, price), 4)
            vals.update(actual=float(a), outcome=res, bet_side=side, price_american=price,
                        profit=prof,
                        close_price_american=(close[1] if side == "over" else close[2])
                        if (close and side) else None,
                        clv=clv(side, p["line"], p["over_price"], p["under_price"], close)
                        if side else None)  # fmt: skip
        old = have.get(p["prop_prediction_id"])
        if old is None:
            conn.execute(insert(prop_grades).values(**vals, graded_at=now))
            run.inserted += 1
        elif any(getattr(old, c) != v for c, v in vals.items()):
            conn.execute(update(prop_grades).where(prop_grades.c.grade_id == old.grade_id)
                         .values(**vals, graded_at=now))  # fmt: skip
            run.updated += 1
    return run


@dataclass
class PropRecord:
    model_version: str
    market: str
    graded: int
    over: int
    under: int
    push: int
    void: int
    bets: int
    wins: int
    losses: int
    profit: float
    avg_clv: float | None


def records(conn: Connection, models: tuple[str, ...] | None = None) -> list[PropRecord]:
    q = select(prop_grades)
    acc = defaultdict(lambda: defaultdict(float))
    clvs = defaultdict(list)
    for r in conn.execute(q):
        if models and not any(r.model_version.startswith(m) for m in models):
            continue
        a = acc[(r.model_version, r.market)]
        a["graded"] += 1
        a[r.outcome] += 1
        if r.bet_side and r.outcome != VOID:
            a["bets"] += 1
            a["wins"] += r.outcome == r.bet_side
            a["losses"] += r.outcome not in (r.bet_side, "push", "no_line")
            a["profit"] += r.profit or 0.0
            if r.clv is not None:
                clvs[(r.model_version, r.market)].append(r.clv)
    out = []
    for (mv, mk), a in sorted(acc.items()):
        c = clvs[(mv, mk)]
        out.append(PropRecord(mv, mk, int(a["graded"]), int(a["over"]), int(a["under"]),
                              int(a["push"]), int(a[VOID]), int(a["bets"]), int(a["wins"]),
                              int(a["losses"]), round(a["profit"], 4),
                              sum(c) / len(c) if c else None))  # fmt: skip
    return out
