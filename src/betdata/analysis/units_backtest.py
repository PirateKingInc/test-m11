"""Backtest of the unit model with and without missing starters, against Elo and the market."""

from __future__ import annotations

import csv
import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import Connection, desc, insert, select

from betdata.analysis.backtest import (
    BREAK_EVEN,
    Pick,
    choose_threshold,
    picks,
    summarize,
    total_picks,
    tune,
    verdict,
    walk_forward,
)
from betdata.analysis.context import GROUPS, Absence, Context
from betdata.analysis.dataset import GameRow
from betdata.analysis.models import EloModel, Prediction
from betdata.analysis.report import _pct, _span, _summary_line
from betdata.analysis.units import (
    GameFeatures,
    UnitModelFit,
    build_features,
    estimate_out_prob,
    fit,
)
from betdata.db.schema import model_predictions

UNIT_GRID = {"alpha": (0.05, 0.1, 0.2), "revert": (0.25, 0.5)}
GROUP_NAMES = {
    "QB": "quarterback", "OL": "offensive line", "WR": "wide receiver", "TE": "tight end",
    "RB": "running back", "DL": "defensive line", "LB": "linebacker", "CB": "cornerback",
    "S": "safety",
}  # fmt: skip


@dataclass
class UnitsResult:
    fits: dict[str, UnitModelFit]  # "units", "units+missing"
    preds: dict[str, list[tuple[GameRow, Prediction | None]]]
    spread_picks: dict[str, tuple[float, list[Pick], list[Pick]]]  # threshold, train, test
    total_picks: dict[str, tuple[float, list[Pick], list[Pick]]]
    features: list[GameFeatures]
    lines: list[str]


def _mae(pairs) -> float | None:
    pairs = list(pairs)
    return sum(abs(a - b) for a, b in pairs) / len(pairs) if pairs else None


def _fmt(x: float | None) -> str:
    return "-" if x is None else f"{x:7.2f}"


def predictions(
    feats: Sequence[GameFeatures], f: UnitModelFit
) -> list[tuple[GameRow, Prediction | None]]:
    return [
        (gf.game, Prediction(f.margin.predict(gf.margin), total=f.total.predict(gf.total)))
        if gf.has_snaps else (gf.game, None)
        for gf in feats
    ]  # fmt: skip


def run_units_backtest(
    games: Sequence[GameRow],
    ctx: Context,
    train: Sequence[int],
    test: Sequence[int],
    *,
    regular_only: bool = False,
    grid: dict[str, Sequence[float]] = UNIT_GRID,
) -> UnitsResult:
    train, test = sorted(train), sorted(test)
    if set(train) & set(test):
        raise ValueError("training and test seasons overlap")
    train_set = set(train)

    # 1. How often a listed starter sits out, from training seasons only.
    log: list = []
    build_features(games, ctx, played_log=log)
    out_prob = estimate_out_prob(log, train_set)

    # 2. Rating speed and season carry-over: smallest training error on the margin.
    best = None
    for alpha, revert in itertools.product(grid["alpha"], grid["revert"]):
        feats = build_features(games, ctx, alpha=alpha, revert=revert, out_prob=out_prob)
        model = fit(feats, train_set, alpha=alpha, revert=revert, out_prob=out_prob, injuries=True)
        err = _mae(
            (model.margin.predict(f.margin), f.game.home_margin)
            for f in feats if f.game.final and f.has_snaps and f.game.season in train_set
        )  # fmt: skip
        if best is None or err < best[0]:
            best = (err, alpha, revert, feats)
    _, alpha, revert, feats = best

    fits = {
        name: fit(feats, train_set, alpha=alpha, revert=revert, out_prob=out_prob, injuries=inj)
        for name, inj in (("units", False), ("units+missing", True))
    }
    preds = {name: predictions(feats, f) for name, f in fits.items()}
    elo_tuned = tune(games, train, regular_only=regular_only)
    elo_preds = dict((g.game_id, p) for g, p in walk_forward(games, EloModel(**elo_tuned.params)))

    # The comparison set: final games with snaps, both closing lines, in each split.
    def comparable(seasons):
        keep = set(seasons)
        return [
            f.game for f in feats
            if f.has_snaps and f.game.final and f.game.season in keep
            and f.game.spread_home is not None and f.game.total is not None
            and (not regular_only or f.game.season_type == "REG")
        ]  # fmt: skip

    by_id = {name: {g.game_id: p for g, p in ps} for name, ps in preds.items()}
    full_id = by_id["units+missing"]
    spread_res, total_res = {}, {}
    for name, ps in preds.items():
        sp = lambda p, t, s: picks(p, t, s, regular_only)  # noqa: E731
        tp = lambda p, t, s: total_picks(p, t, s, regular_only)  # noqa: E731
        t_spread, _ = choose_threshold(sp, ps, train)
        t_total, _ = choose_threshold(tp, ps, train)
        spread_res[name] = (t_spread, sp(ps, t_spread, train), sp(ps, t_spread, test))
        total_res[name] = (t_total, tp(ps, t_total, train), tp(ps, t_total, test))

    full = fits["units+missing"]
    lines = [
        "NFL unit model with missing starters · "
        f"alpha={alpha:g} revert={revert:g} (chosen on {_span(train)})",
        f"Listed starters who sat out ({_span(train)}): Questionable "
        f"{_pct(out_prob['Questionable'], 0)}, Doubtful {_pct(out_prob['Doubtful'], 0)}, "
        "Out 100%",
        "",
        "Average miss on the final margin (games with snap counts and closing lines):",
        f"  {'':22} {'units':>7} {'+missing':>8} {'elo':>7} {'market':>7} {'games':>6}",
    ]
    for label, seasons in ((f"train {_span(train)}", train), (f"test  {_span(test)}", test)):
        gs = comparable(seasons)
        lines.append(
            f"  {label:22} "
            f"{_fmt(_mae((by_id['units'][g.game_id].home_margin, g.home_margin) for g in gs))} "
            f"{_fmt(_mae((full_id[g.game_id].home_margin, g.home_margin) for g in gs))} "
            f"{_fmt(_mae((elo_preds[g.game_id].home_margin, g.home_margin) for g in gs))} "
            f"{_fmt(_mae((-g.spread_home, g.home_margin) for g in gs))} {len(gs):6}"
        )  # fmt: skip
    lines += [
        "",
        "Average miss on total points:",
        f"  {'':22} {'units':>7} {'+missing':>8} {'':>7} {'market':>7}",
    ]
    for label, seasons in ((f"train {_span(train)}", train), (f"test  {_span(test)}", test)):
        gs = comparable(seasons)
        pts = lambda g: g.home_score + g.away_score  # noqa: E731
        lines.append(
            f"  {label:22} "
            f"{_fmt(_mae((by_id['units'][g.game_id].total, pts(g)) for g in gs))} "
            f"{_fmt(_mae((by_id['units+missing'][g.game_id].total, pts(g)) for g in gs))} "
            f"{'':>7} {_fmt(_mae((g.total, pts(g)) for g in gs))}"
        )  # fmt: skip

    header = (f"  {'':22} {'bets':>5}  {'W-L-P':<13} {'win%':>6}  {'95% range':>12}  "
              f"{'ROI':>6}  {'p':>6}")  # fmt: skip
    for market, res in (("Spread", spread_res), ("Total", total_res)):
        lines += [
            "",
            f"{market} bets at the closing line (break-even {_pct(BREAK_EVEN, 2)}):",
            header,
        ]
        for name in ("units", "units+missing"):
            t, tr, te = res[name]
            lines.append(_summary_line(f"{name} >= {t:g} pts", summarize(tr)) + "   train")
            lines.append(_summary_line(f"{name} >= {t:g} pts", summarize(te)) + "   test")

    lines += [
        "",
        f"What a missing starter is worth (fitted on {_span(train)}; one full-time starter out):",
        f"  {'':16} {'margin':>8} {'total':>8}",
    ]
    for grp in GROUPS:
        m = -full.margin.raw_coef(f"miss_{grp}")  # for the team missing him
        t = full.total.raw_coef(f"miss_{grp}_sum")
        lines.append(f"  {GROUP_NAMES[grp]:16} {m:+8.2f} {t:+8.2f}")
    lines += [
        f"  {'2nd DB out (extra)':16} {-full.margin.raw_coef('secondary_sq') * 3:+8.2f} "
        f"{full.total.raw_coef('secondary_sq_sum') * 3:+8.2f}",
        "  margin: points for the team missing him (negative = hurts it); total: change in "
        "combined points. 2nd DB: extra effect of a second defensive back out on top of the "
        "first (from the squared term).",
        "",
        f"Spreads (+missing): {verdict(summarize(spread_res['units+missing'][2]))}",
        f"Totals  (+missing): {verdict(summarize(total_res['units+missing'][2]))}",
        "Only what was known before kickoff is used: snap counts from earlier games, the injury "
        "report and roster status (injured reserve, released). Game-day inactives are not used.",
    ]
    return UnitsResult(fits, preds, spread_res, total_res, feats, lines)


CSV_COLUMNS = [
    "model", "market", "split", "season", "week", "kickoff_utc", "home", "away", "line",
    "model_number", "edge", "side", "price", "home_score", "away_score", "result", "profit",
]  # fmt: skip


def write_units_csv(result: UnitsResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for market, res in (("spread", result.spread_picks), ("total", result.total_picks)):
            for name, (_, tr, te) in res.items():
                for split, ps in (("train", tr), ("test", te)):
                    for p in ps:
                        g = p.game
                        w.writerow([
                            name, market, split, g.season, g.week, g.start.isoformat(), g.home,
                            g.away, p.line, round(p.pred_margin, 2), round(p.edge, 2), p.side,
                            p.price, g.home_score, g.away_score, p.result, round(p.profit, 4),
                        ])  # fmt: skip
    return path


# --- Live predictions ----------------------------------------------------------------------


@dataclass(frozen=True)
class UnitLivePick:
    game: GameRow
    home_margin: float
    total: float
    home_out: list[Absence]
    away_out: list[Absence]
    stored: bool


def predict_units_upcoming(
    conn: Connection,
    games: Sequence[GameRow],
    ctx: Context,
    *,
    now: datetime,
    until: datetime,
    train: Sequence[int],
    alpha: float,
    revert: float,
    lines_source: str,
) -> tuple[UnitModelFit, list[UnitLivePick]]:
    """Fit the unit model with missing starters on `train`, predict games kicking off in
    (now, until], and store each prediction when it or the market moved (change-only)."""
    train_set = set(train)
    log: list = []
    build_features(games, ctx, played_log=log)
    out_prob = estimate_out_prob(log, train_set)
    feats = build_features(games, ctx, alpha=alpha, revert=revert, out_prob=out_prob)
    model = fit(feats, train_set, alpha=alpha, revert=revert, out_prob=out_prob, injuries=True)
    mv = model.version
    out = []
    for gf in feats:
        g = gf.game
        if g.final or not gf.has_snaps or not (now < g.start <= until):
            continue
        margin = round(model.margin.predict(gf.margin), 2)
        total = round(model.total.predict(gf.total), 2)
        last = conn.execute(
            select(
                model_predictions.c.pred_home_margin, model_predictions.c.pred_total,
                model_predictions.c.market_spread_home, model_predictions.c.market_total,
            )
            .where(
                model_predictions.c.model_version == mv, model_predictions.c.game_id == g.game_id
            )
            .order_by(desc(model_predictions.c.predicted_at))
            .limit(1)
        ).first()  # fmt: skip
        changed = last is None or tuple(last) != (margin, total, g.spread_home, g.total)
        if changed:
            conn.execute(
                insert(model_predictions).values(
                    model="units+missing", model_version=mv, game_id=g.game_id,
                    predicted_at=now, pred_home_margin=margin, pred_total=total,
                    market_spread_home=g.spread_home, market_total=g.total,
                    market_source=lines_source if g.spread_home is not None else None,
                    params={"alpha": alpha, "revert": revert, "train": [min(train), max(train)],
                            "out_prob": model.out_prob},
                )
            )  # fmt: skip
        out.append(UnitLivePick(g, margin, total, gf.home_out, gf.away_out, changed))
    return model, out
