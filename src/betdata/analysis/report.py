"""Backtest report (text + CSV) and live prediction storage."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import Connection, desc, insert, select

from betdata.analysis.backtest import (
    BREAK_EVEN,
    MIN_TRAIN_BETS,
    Pick,
    Summary,
    Tuned,
    mae,
    picks,
    summarize,
    tune,
    verdict,
    walk_forward,
)
from betdata.analysis.dataset import GameRow
from betdata.analysis.models import MODELS, Model, version
from betdata.db.schema import model_predictions


@dataclass
class BacktestResult:
    model: Model
    tuned: Tuned | None  # None when parameters were given, not chosen
    threshold: float
    train: list[int]
    test: list[int]
    train_picks: list[Pick]
    test_picks: list[Pick]
    lines: list[str]


def _pct(x: float | None, digits: int = 1) -> str:
    return "-" if x is None else f"{100 * x:.{digits}f}%"


def _summary_line(label: str, s: Summary) -> str:
    ci = s.ci95()
    rng = "-" if ci is None else f"{100 * ci[0]:.1f}-{100 * ci[1]:.1f}%"
    p = s.p_value()
    return (
        f"  {label:22} {s.bets:5}  {f'{s.wins}-{s.losses}-{s.pushes}':<13} {_pct(s.win_rate):>6}  "
        f"{rng:>12}  {_pct(s.roi):>6}  {'-' if p is None else f'{p:.3f}':>6}"
    )


def _span(seasons: Sequence[int]) -> str:
    return f"{min(seasons)}-{max(seasons)}" if len(seasons) > 1 else str(seasons[0])


def run_backtest(
    games: Sequence[GameRow],
    model_name: str,
    train: Sequence[int],
    test: Sequence[int],
    *,
    params: dict | None = None,
    threshold: float | None = None,
    regular_only: bool = False,
) -> BacktestResult:
    """Choose parameters/threshold on `train` (unless given), then score `test` once."""
    make = MODELS[model_name]
    train, test = sorted(train), sorted(test)
    if set(train) & set(test):
        raise ValueError("training and test seasons overlap")
    tuned = None
    if model_name == "elo" and params is None:
        tuned = tune(games, train, make, regular_only=regular_only)
        params = tuned.params
    model = make(**(params or {}))
    if threshold is None:
        threshold = tuned.threshold if tuned else 2.0
    preds = walk_forward(games, model)
    train_picks = picks(preds, threshold, train, regular_only)
    test_picks = picks(preds, threshold, test, regular_only)

    first = min(g.season for g in games)
    lines = [
        f"NFL spread backtest · {version(model)}",
        f"Bet rule: back the model's side when it differs from the closing spread by "
        f">= {threshold:g} points" + (f" (chosen on {_span(train)})" if tuned else ""),
        f"Ratings start in {first}; seasons before {min(train)} only warm them up.",
        "",
        "Accuracy on final margin (all games with a closing line):",
        f"  {'':22} {'model':>7} {'market':>7} {'games':>6}",
    ]
    for label, seasons in (("train " + _span(train), train), ("test  " + _span(test), test)):
        m_err, c_err, n = mae(preds, seasons)
        if n:
            lines.append(f"  {label:22} {m_err:7.2f} {c_err:7.2f} {n:6}")
    lines += [
        "",
        f"Bets at the closing line (break-even {_pct(BREAK_EVEN, 2)} at -110):",
        f"  {'':22} {'bets':>5}  {'W-L-P':<13} {'win%':>6}  {'95% range':>12}  {'ROI':>6}  "
        f"{'p':>6}",
        _summary_line("train " + _span(train), summarize(train_picks)),
        _summary_line("test  " + _span(test), summarize(test_picks)),
        "",
        "Test seasons one by one:",
    ]
    for season in test:
        lines.append(
            _summary_line(str(season), summarize(p for p in test_picks if p.game.season == season))
        )
    if tuned:
        lines += [
            "",
            f"Thresholds tried on {_span(train)} (min {MIN_TRAIN_BETS} bets to be chosen):",
        ]
        lines += [_summary_line(f">= {t:g} pts", s) for t, s in tuned.by_threshold]
    lines += ["", f"Verdict: {verdict(summarize(test_picks))}"]
    lines.append(
        "p = chance of doing at least this well with no edge. Lines are nflverse's (book "
        "unspecified); prices default to -110 where the file has none."
    )
    return BacktestResult(model, tuned, threshold, train, test, train_picks, test_picks, lines)


CSV_COLUMNS = [
    "split", "season", "season_type", "week", "kickoff_utc", "home", "away", "spread_home",
    "model_margin", "edge", "side", "line", "price", "home_score", "away_score", "result",
    "profit",
]  # fmt: skip


def write_csv(result: BacktestResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for split, ps in (("train", result.train_picks), ("test", result.test_picks)):
            for p in ps:
                g = p.game
                w.writerow([
                    split, g.season, g.season_type, g.week, g.start.isoformat(), g.home, g.away,
                    g.spread_home, round(p.pred_margin, 2), round(p.edge, 2), p.side, p.line,
                    p.price, g.home_score, g.away_score, p.result, round(p.profit, 4),
                ])  # fmt: skip
    return path


# --- Live predictions --------------------------------------------------------


@dataclass(frozen=True)
class LivePrediction:
    game: GameRow
    home_margin: float
    home_win_prob: float | None
    stored: bool  # False when unchanged since the last stored row


def predict_upcoming(
    conn: Connection,
    games: Sequence[GameRow],
    model: Model,
    *,
    now: datetime,
    until: datetime,
    lines_source: str,
) -> list[LivePrediction]:
    """Rate every final game, predict games kicking off in (now, until], and store each
    prediction when it or the market line changed since the last stored one."""
    mv = version(model)
    out = []
    for g, p in walk_forward(games, model):
        if p is None or g.final or not (now < g.start <= until):
            continue
        last = conn.execute(
            select(model_predictions.c.pred_home_margin, model_predictions.c.market_spread_home)
            .where(
                model_predictions.c.model_version == mv, model_predictions.c.game_id == g.game_id
            )
            .order_by(desc(model_predictions.c.predicted_at))
            .limit(1)
        ).first()
        margin = round(p.home_margin, 2)
        changed = last is None or (last.pred_home_margin, last.market_spread_home) != (
            margin,
            g.spread_home,
        )
        if changed:
            conn.execute(
                insert(model_predictions).values(
                    model=model.name,
                    model_version=mv,
                    game_id=g.game_id,
                    predicted_at=now,
                    pred_home_margin=margin,
                    pred_home_win_prob=None
                    if p.home_win_prob is None
                    else round(p.home_win_prob, 4),
                    market_spread_home=g.spread_home,
                    market_source=lines_source if g.spread_home is not None else None,
                    params=model.params(),
                )
            )
        out.append(LivePrediction(g, margin, p.home_win_prob, changed))
    return out
