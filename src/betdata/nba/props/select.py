"""NBA prop model selection on validation (MODEL.md, M15): error vs the last-10 average per
market, calibration of over probabilities, and the market backtest once lines are loaded."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from betdata.nba.pipeline import TEST, TRAIN, VALIDATE
from betdata.nba.props import model as M
from betdata.nba.props.panel import MARKETS

log = logging.getLogger(__name__)
FEATURES_CACHE = Path("data/derived/nba_prop_features.pkl")
REPORTS = Path("reports/nba")
BINS = (0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0)


def history_features(rebuild: bool = False) -> pd.DataFrame:
    """Features and actuals of every played player-game 2015-16 to 2025-26 at both points
    (cached under data/derived; built from the database and the M14 feature rows)."""
    if FEATURES_CACHE.exists() and not rebuild:
        return pd.read_pickle(FEATURES_CACHE)
    from betdata.nba.cli import _engine, _rows
    from betdata.nba.data import outcomes
    from betdata.nba.live import Design, load_fit
    from betdata.nba.props.panel import Panel
    from betdata.nba.props.side import side_history

    store, out, rows, _ = _rows()
    _, engine = _engine()
    with engine.connect() as conn:
        panel = Panel.load(conn)
    side = side_history(rows, outcomes(store))
    play = load_fit(Design.load())[1]
    tg = panel.played_targets(seasons=range(2015, 2026))
    f = panel.features(tg, side, play)
    y = panel.p.set_index(["game_id", "player_id"])[["minutes", "pts", "reb", "ast", "fg3m",
                                                     "pra"]]  # fmt: skip
    f = f.join(y, on=["game_id", "player_id"])
    FEATURES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    f.to_pickle(FEATURES_CACHE)
    return f


def usable(f: pd.DataFrame) -> pd.DataFrame:
    return M.prepare(f[f.has_state & f.min_l10.notna()])


def calibration(p: np.ndarray, y: np.ndarray) -> list[dict]:
    b = pd.cut(p, list(BINS))
    t = pd.DataFrame({"p": p, "y": y, "b": b}).groupby("b", observed=True).agg(
        n=("y", "size"), pred=("p", "mean"), actual=("y", "mean"))  # fmt: skip
    return [{"bin": str(i), "n": int(r.n), "pred": round(float(r.pred), 3),
             "actual": round(float(r.actual), 3)} for i, r in t.iterrows()]  # fmt: skip


def synthetic_line(f: pd.DataFrame, stat: str) -> np.ndarray:
    """A half-point line just under the last-10 average (for calibration where no market line
    exists)."""
    return np.floor(f[f"g10_{stat}"].to_numpy()) + 0.5


def score(mod: M.PropModel, va: pd.DataFrame) -> dict:
    m_hat = mod.predict_minutes(va)
    out = {"minutes": {"mae": float(np.abs(m_hat - va.minutes).mean()),
                       "mae_last10": float(np.abs(va.min_l10 - va.minutes).mean())}}  # fmt: skip
    for mk, s in MARKETS.items():
        mu = mod.predict(va, mk, m_hat)
        y = va[s].to_numpy()
        base = va[f"g10_{s}"].to_numpy()
        line = synthetic_line(va, s)
        p = mod.p_over(mk, mu, line)
        hit = (y > line).astype(float)
        out[mk] = {"n": int(len(y)), "mae": float(np.abs(mu - y).mean()),
                   "mae_last10": float(np.abs(base - y).mean()),
                   "rmse": float(np.sqrt(np.mean((mu - y) ** 2))),
                   "rmse_last10": float(np.sqrt(np.mean((base - y) ** 2))),
                   "brier_synthetic": float(np.mean((p - hit) ** 2)),
                   "brier_constant": float(np.mean((hit.mean() - hit) ** 2)),
                   "calibration_synthetic": calibration(p, hit)}  # fmt: skip
    return out


def select(rebuild: bool = False, adjust: str | None = None) -> dict:
    f = usable(history_features(rebuild))
    tr, va = f[f.season.isin(TRAIN)], f[f.season.isin(VALIDATE)]
    mod = M.fit(tr)
    res = {"adjustment": adjust or "full build", "train": list(TRAIN),
           "validate": list(VALIDATE), "test_untouched": list(TEST),
           "validation": score(mod, va), "coef": {
               "minutes": mod.minutes.raw_coef(),
               **{mk: r.raw_coef() for mk, r in mod.stats.items()}},
           "dispersion": {mk: d.to_dict() for mk, d in mod.disp.items()}}  # fmt: skip
    REPORTS.mkdir(parents=True, exist_ok=True)
    name = "props_selection.json" if not adjust else f"props_selection_{adjust}.json"
    (REPORTS / name).write_text(json.dumps(res, indent=1, default=float) + "\n")
    return res


# --- market lines: calibration, thresholds (validation 2022-23), final test -----------------

EDGE_THRESHOLDS = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)
MIN_BETS = 100
BACKTESTED = ("points", "rebounds", "assists", "threes")  # lines exist in validation (2022-23)
DESIGN_PATH = Path("config/nba_props.json")


def history_lines(conn) -> pd.DataFrame:
    """Historical open and close prop lines per (game, player, market): one book per game, the
    most preferred one that has both sides priced."""
    from sqlalchemy import select as sel

    from betdata.db.schema import books, prop_lines
    from betdata.props.lines import BOOK_ORDER

    t = prop_lines
    df = pd.read_sql(sel(t.c.game_id, t.c.player_id, t.c.market, t.c.kind, t.c.line,
                         t.c.over_price, t.c.under_price, books.c.key.label("book"))
                     .join(books, books.c.book_id == t.c.book_id)
                     .where(t.c.kind.in_(("open", "close")), t.c.game_id.is_not(None)),
                     conn)  # fmt: skip
    df = df.dropna(subset=["line", "over_price", "under_price"])
    df["rank"] = df.book.map({b: i for i, b in enumerate(BOOK_ORDER)}).fillna(len(BOOK_ORDER))
    df = df.sort_values("rank").drop_duplicates(["game_id", "player_id", "market", "kind"])
    wide = df.pivot_table(index=["game_id", "player_id", "market"], columns="kind",
                          values=["line", "over_price", "under_price"], aggfunc="first")
    wide.columns = [f"{k}_{v}" for v, k in wide.columns]
    return wide.reset_index()


def _novig_over(o, u) -> np.ndarray:
    from betdata.moneyline import no_vig

    out = []
    for a, b in zip(o, u, strict=True):
        nv = no_vig(int(a), int(b)) if a == a and b == b else None
        out.append(nv[0] if nv else np.nan)
    return np.asarray(out, dtype=float)


def priced(mod: M.PropModel, f: pd.DataFrame, lines: pd.DataFrame, market: str,
           at: str) -> pd.DataFrame:  # fmt: skip
    """Predictions joined with the line `at` ("open" | "close"): P(over | no push), the no-vig
    over probability, edge and the actual."""
    from betdata.nba.props.live import p_over_no_push

    s = MARKETS[market]
    li = lines[lines.market == market].dropna(subset=[f"{at}_line"])
    d = f.merge(li, on=["game_id", "player_id"], how="inner")
    if d.empty:
        return d
    m_hat = mod.predict_minutes(d)
    d["mu"] = mod.predict(d, market, m_hat)
    d["line"] = d[f"{at}_line"]
    d["over_price"], d["under_price"] = d[f"{at}_over_price"], d[f"{at}_under_price"]
    d["p"] = p_over_no_push(mod, market, d.mu.to_numpy(), d.line.to_numpy())
    d["nv"] = _novig_over(d.over_price, d.under_price)
    d["edge"] = 100 * (d.p - d.nv)
    d["actual"] = d[s]
    d["over"] = np.where(d.actual > d.line, 1.0, np.where(d.actual < d.line, 0.0, np.nan))
    return d.dropna(subset=["nv"])


def bets(d: pd.DataFrame, threshold: float) -> pd.DataFrame:
    from betdata.moneyline import profit

    b = d[d.edge.abs() >= threshold].copy()
    side_over = b.edge > 0
    price = np.where(side_over, b.over_price, b.under_price)
    res = np.where(b.over.isna(), "push", np.where((b.over == 1) == side_over, "win", "loss"))
    b["result"] = res
    b["profit"] = [profit(r, p) for r, p in zip(res, price, strict=True)]
    return b


def record(b: pd.DataFrame) -> dict:
    from betdata.nba.selection import wilson

    w, lo = int((b.result == "win").sum()), int((b.result == "loss").sum())
    n = len(b)
    ci = wilson(w, w + lo)
    return {"bets": n, "w": w, "l": lo, "p": int((b.result == "push").sum()),
            "win_rate": round(w / (w + lo), 4) if w + lo else None,
            "win_rate_95": [round(ci[0], 4), round(ci[1], 4)] if w + lo else None,
            "roi": round(float(b.profit.sum()) / n, 4) if n else None}  # fmt: skip


def line_calibration(d: pd.DataFrame) -> dict:
    ok = d.over.notna()
    p, y, nv = d.p[ok].to_numpy(), d.over[ok].to_numpy(), d.nv[ok].to_numpy()
    from betdata.moneyline import fit_platt

    slope, intercept = fit_platt(p, y)
    return {"n": int(ok.sum()), "brier_model": float(np.mean((p - y) ** 2)),
            "brier_novig": float(np.mean((nv - y) ** 2)), "platt_slope": float(slope),
            "platt_intercept": float(intercept), "table": calibration(p, y)}  # fmt: skip


def miscalibrated(c: dict) -> bool:
    """One Platt fit is applied only if the over probabilities are clearly off: slope outside
    0.85-1.15 or an intercept beyond ±0.1 (on validation lines)."""
    return not (0.85 <= c["platt_slope"] <= 1.15) or abs(c["platt_intercept"]) > 0.1


def tune_lines(conn) -> dict:
    """Validation 2022-23 (the only validation season with lines): calibration at the market
    line, a calibration fit only if miscalibrated, and an edge threshold per backtested market
    (graded at the close, the M14 convention)."""
    f = usable(history_features())
    tr = f[f.season.isin(TRAIN)]
    va = f[f.season.isin((2022,))]
    mod = M.fit(tr)
    lines = history_lines(conn)
    out = {"lines_rows": int(len(lines)), "markets": {}}
    for mk in MARKETS:
        d = priced(mod, va, lines, mk, "close")
        if d.empty or mk not in BACKTESTED:
            out["markets"][mk] = {"validation_rows": int(len(d)), "backtest": None,
                                  "calibration": line_calibration(d) if len(d) > 50 else None}
            continue
        cal = line_calibration(d)
        if miscalibrated(cal):
            mod.calib[mk] = (cal["platt_slope"], cal["platt_intercept"])
            d = priced(mod, va, lines, mk, "close")
        by = {t: record(bets(d, t)) for t in EDGE_THRESHOLDS}
        ok = [t for t in EDGE_THRESHOLDS if by[t]["bets"] >= MIN_BETS]
        th = max(ok, key=lambda t: (by[t]["roi"], -t)) if ok else None
        out["markets"][mk] = {"validation_rows": int(len(d)), "calibration": cal,
                              "calibration_applied": mk in mod.calib,
                              "platt": mod.calib.get(mk), "thresholds": by, "threshold": th}
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "props_tune.json").write_text(json.dumps(out, indent=1, default=float) + "\n")
    return out


def final_test(conn, again: bool = False) -> dict:
    """Scored once: fit on 2015-16 to 2022-23, predict 2023-24 to 2025-26; pre-tip bets at the
    close line and price, morning bets at the opening line and price; error vs the line."""
    path = REPORTS / "props_final_test.json"
    if path.exists() and not again:
        raise SystemExit(f"{path} exists: the final test is scored once (--again to reproduce)")
    tune = json.loads((REPORTS / "props_tune.json").read_text())
    f = usable(history_features())
    mod = M.fit(f[f.season.isin((*TRAIN, *VALIDATE))])
    for mk, r in tune["markets"].items():
        if r.get("calibration_applied"):
            mod.calib[mk] = tuple(r["platt"])
    te = f[f.season.isin(TEST)]
    lines = history_lines(conn)
    res = {"seasons": list(TEST), "markets": {}}
    m_hat = mod.predict_minutes(te)
    for mk, s in MARKETS.items():
        mu = mod.predict(te, mk, m_hat)
        y = te[s].to_numpy()
        base = te[f"g10_{s}"].to_numpy()
        r = {"n": int(len(y)), "mae": float(np.abs(mu - y).mean()),
             "mae_last10": float(np.abs(base - y).mean())}  # fmt: skip
        th = (tune["markets"].get(mk) or {}).get("threshold")
        for at, point in (("close", "pretip"), ("open", "morning")):
            d = priced(mod, te[te.point == point], lines, mk, at)
            if d.empty:
                r[f"{point}_at_{at}"] = None
                continue
            ok = d.over.notna()
            r[f"{point}_at_{at}"] = {
                "rows": int(len(d)),
                "mae_model": float(np.abs(d.mu - d.actual).mean()),
                "mae_line": float(np.abs(d.line - d.actual).mean()),
                "brier_model": float(np.mean((d.p[ok] - d.over[ok]) ** 2)),
                "brier_novig": float(np.mean((d.nv[ok] - d.over[ok]) ** 2)),
                "calibration": calibration(d.p[ok].to_numpy(), d.over[ok].to_numpy()),
                "bets": record(bets(d, th)) if th is not None and mk in BACKTESTED else None,
                "by_season": {int(sn): record(bets(g, th)) for sn, g in d.groupby("season")}
                if th is not None and mk in BACKTESTED else None}  # fmt: skip
        res["markets"][mk] = r
    res["model"] = {"calib": {k: list(v) for k, v in mod.calib.items()}}
    path.write_text(json.dumps(res, indent=1, default=float) + "\n")
    return res


def freeze() -> Path:
    """Fit the prop model on every completed season (2015-16 to 2025-26) with the validated
    calibration; write config/fits/nba_props_2015_2025.json and config/nba_props.json."""
    from betdata.config import PROJECT_ROOT

    tune = json.loads((REPORTS / "props_tune.json").read_text())
    test = json.loads((REPORTS / "props_final_test.json").read_text())
    f = usable(history_features())
    mod = M.fit(f[f.season.between(2015, 2025)])
    for mk, r in tune["markets"].items():
        if r.get("calibration_applied"):
            mod.calib[mk] = tuple(r["platt"])
    fit_path = PROJECT_ROOT / "config" / "fits" / "nba_props_2015_2025.json"
    fit_path.write_text(json.dumps({"seasons": [2015, 2025], "model": mod.to_dict()},
                                   indent=1) + "\n")  # fmt: skip
    thresholds = {mk: (tune["markets"].get(mk) or {}).get("threshold") for mk in BACKTESTED}
    test_roi = {}
    for mk in BACKTESTED:
        b = ((test["markets"].get(mk) or {}).get("pretip_at_close") or {}).get("bets")
        test_roi[mk] = b["roi"] if b else None
    design = {"fit": str(fit_path.relative_to(PROJECT_ROOT)),
              "thresholds": {k: v for k, v in thresholds.items() if v is not None},
              "test_roi": test_roi,
              "research_only": [m for m in MARKETS if m not in BACKTESTED
                                or thresholds.get(m) is None]}  # fmt: skip
    (PROJECT_ROOT / DESIGN_PATH).write_text(json.dumps(design, indent=2) + "\n")
    return fit_path
