"""M20 Parts C, E, F: the engine's probabilities at stored market lines, the market blend and
calibration, the pick rule, and the three checks (MODEL.md "M20").

The engine predicts first; market lines are joined afterwards (this module may read them; the
engine modules may not). Sides: ESPN's stored closing lines (the only pre-tip line in 2021-23;
openers 2023-24 on). Props: ESPN's stored opening prop lines (no-vig over 0.35-0.65)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from betdata.nba.engine import sim as SM

PROP_STATS = {"points": "pts", "rebounds": "reb", "assists": "ast", "threes": "fg3m",
              "pra": "pra"}  # fmt: skip
W_GRID = tuple(round(x / 10, 1) for x in range(11))
THETAS = (0.02, 0.03, 0.05, 0.075, 0.10)
MIN_PICKS = 200


def dec(american) -> np.ndarray:
    a = np.asarray(american, float)
    return np.where(a > 0, 1 + a / 100, 1 + 100 / np.abs(a))


def novig(a, b) -> np.ndarray:
    """No-vig probability of side a from American prices a, b (proportional)."""
    pa, pb = 1 / dec(a), 1 / dec(b)
    return pa / (pa + pb)


def side_lines(conn, g: pd.DataFrame) -> pd.DataFrame:
    """Per game: the closing home spread, moneylines and total, and the opening ones (if any)."""
    from betdata.nba.data import load_odds

    frame, close = load_odds(conn, g)
    c = close.rename(columns={"spread_home": "c_spread", "ml_home": "c_ml_home",
                              "ml_away": "c_ml_away", "total": "c_total", "book": "c_book"})
    op = frame[frame.kind == "open"].drop_duplicates("game_id").set_index("game_id")
    o = op[["spread_home", "ml_home", "ml_away", "total"]].rename(
        columns={"spread_home": "o_spread", "ml_home": "o_ml_home", "ml_away": "o_ml_away",
                 "total": "o_total"})  # fmt: skip
    return c.join(o, how="left")


def simulate(eng, gids, prop_lines: pd.DataFrame | None, side: pd.DataFrame | None,
             n_sims: int = SM.N_SIMS, point: str = "pretip", log=None):  # fmt: skip
    """(game rows, prop rows) of engine probabilities at the given lines."""
    d = eng.predict(gids, (point,))
    games, props = [], []
    pl = None if prop_lines is None else prop_lines.set_index("game_id")
    for i, (gid, rows) in enumerate(d.groupby("game_id")):
        s = SM.sim_game(rows, eng.fit, n_sims, seed=int(gid))
        r = {"game_id": gid, "mu_margin": float(s.margin.mean()),
             "mu_total": float(s.total.mean()), "sd_margin": float(s.margin.std()),
             "p_home_win": s.p_home_win(), "checks_ok": all(s.checks.values())}
        if side is not None and gid in side.index:
            x = side.loc[gid]
            for pre in ("c", "o"):
                if pd.notna(x.get(f"{pre}_spread")):
                    r[f"{pre}_p_cover"], r[f"{pre}_p_spush"] = s.p_cover(float(x[f"{pre}_spread"]))
                if pd.notna(x.get(f"{pre}_total")):
                    r[f"{pre}_p_over"], r[f"{pre}_p_tpush"] = s.p_over(float(x[f"{pre}_total"]))
        games.append(r)
        if pl is not None and gid in pl.index:
            sub = pl.loc[[gid]]
            idx = {pid: j for j, pid in enumerate(s.players.player_id.astype(str))}
            for q in sub.itertuples():
                j = idx.get(str(q.player_id))
                stat = PROP_STATS.get(q.market)
                if j is None or stat is None:
                    continue
                po = s.player_p_over(j, stat, float(q.line))
                if po is None:
                    continue
                dist = s.player_dist(j, stat)
                props.append({"game_id": gid, "player_id": str(q.player_id),
                              "market": q.market, "line": float(q.line),
                              "p_over": po[0], "p_push": po[1], "mean": dist["mean"],
                              "median": dist["median"],
                              "minutes": s.player_dist(j, "minutes")["mean"]})  # fmt: skip
        if log and i % 200 == 0:
            log(f"  simulated {i} games")
    return pd.DataFrame(games), pd.DataFrame(props), d


# --- Part C: blend and calibration ------------------------------------------------------------

def logloss(p, y) -> np.ndarray:
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def choose_weight(p_eng, p_mkt, y) -> tuple[float, list]:
    """w in W_GRID minimising validation log loss of w p_eng + (1 - w) p_mkt; ties go to the
    smaller w (the market)."""
    rows, best = [], None
    for w in W_GRID:
        ll = float(logloss(w * p_eng + (1 - w) * p_mkt, y).mean())
        rows.append({"w": w, "logloss": ll})
        if best is None or ll < best[1] - 1e-6:
            best = (w, ll)
    return best[0], rows


def platt_fit(p, y) -> tuple[float, float, float]:
    """Logistic regression of y on logit(p) (unpenalised): (slope, intercept, slope SE)."""
    from sklearn.linear_model import LogisticRegression

    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    x = np.log(p / (1 - p))
    y = np.asarray(y, float)
    m = LogisticRegression(C=1e10, max_iter=1000).fit(x.reshape(-1, 1), y)
    a, b = float(m.coef_[0][0]), float(m.intercept_[0])
    mu = 1 / (1 + np.exp(-(a * x + b)))
    X = np.column_stack([x, np.ones_like(x)])
    H = X.T @ (X * (mu * (1 - mu))[:, None])
    return a, b, float(math.sqrt(np.linalg.inv(H)[0, 0]))


def apply_platt(p, cal: tuple[float, float] | None) -> np.ndarray:
    if not cal:
        return np.asarray(p, float)
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    z = cal[0] * np.log(p / (1 - p)) + cal[1]
    return 1 / (1 + np.exp(-z))


def calibrate(p, y) -> tuple[tuple | None, dict]:
    """One Platt fit; applied only if the slope differs from 1 by more than 2 SE."""
    a, b, se = platt_fit(p, y)
    use = abs(a - 1) > 2 * se
    return ((a, b) if use else None), {"slope": a, "intercept": b, "se": se, "applied": use}


# --- Part E: the pick rule --------------------------------------------------------------------

def picks(fair_a, price_a, price_b) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For two-sided markets: (EV of the best side, side 0/1, its price)."""
    ev_a = fair_a * dec(price_a) - 1
    ev_b = (1 - fair_a) * dec(price_b) - 1
    side = (ev_b > ev_a).astype(int)
    return np.maximum(ev_a, ev_b), side, np.where(side == 0, price_a, price_b)


def roi(won, push, price) -> float:
    prof = np.where(push, 0.0, np.where(won, dec(price) - 1, -1.0))
    return float(prof.mean()) if len(prof) else float("nan")


def choose_theta(ev, won, push, price) -> tuple[float, bool, list]:
    rows, best = [], None
    for th in THETAS:
        sel = ev >= th
        n = int(sel.sum())
        r = roi(won[sel], push[sel], price[sel]) if n else float("nan")
        rows.append({"theta": th, "picks": n, "roi": r})
        if n >= MIN_PICKS and (best is None or r > best[1]):
            best = (th, r)
    if best is None:
        best = (THETAS[0], rows[0]["roi"])
    return best[0], bool(best[1] > 0), rows


# --- assembling the scored rows -----------------------------------------------------------------

GAME_MARKETS = ("spreads", "totals", "h2h")
PROP_MARKETS = ("points", "rebounds", "assists", "threes", "pra")


def game_rows(games: pd.DataFrame, side: pd.DataFrame, results: pd.DataFrame,
              at: str = "c") -> pd.DataFrame:  # fmt: skip
    """One row per (game, market) at the `at` line ("c" close, "o" open): the engine's
    probability of side a (home / over), the market no-vig, the outcome and both prices."""
    g = games.set_index("game_id").join(side, how="inner").join(results, how="inner")
    out = []
    for gid, r in g.iterrows():
        if pd.notna(r.get(f"{at}_spread")) and pd.notna(r.get(f"{at}_p_cover")):
            x = r.margin + r[f"{at}_spread"]
            if x != 0:
                pe = r[f"{at}_p_cover"] / max(1 - r[f"{at}_p_spush"], 1e-9)
                out.append((gid, "spreads", pe, 0.5, float(x > 0), -110, -110,
                            r[f"{at}_spread"]))  # fmt: skip
        if pd.notna(r.get(f"{at}_total")) and pd.notna(r.get(f"{at}_p_over")):
            x = r.total - r[f"{at}_total"]
            if x != 0:
                pe = r[f"{at}_p_over"] / max(1 - r[f"{at}_p_tpush"], 1e-9)
                out.append((gid, "totals", pe, 0.5, float(x > 0), -110, -110, r[f"{at}_total"]))
        if pd.notna(r.get(f"{at}_ml_home")) and pd.notna(r.get(f"{at}_ml_away")):
            pm = float(novig(r[f"{at}_ml_home"], r[f"{at}_ml_away"]))
            out.append((gid, "h2h", r.p_home_win, pm, float(r.margin > 0), r[f"{at}_ml_home"],
                        r[f"{at}_ml_away"], np.nan))  # fmt: skip
    return pd.DataFrame(out, columns=["game_id", "market", "p_eng", "p_mkt", "y", "price_a",
                                      "price_b", "line"])  # fmt: skip


def prop_rows(props: pd.DataFrame, plines: pd.DataFrame, actual: pd.DataFrame) -> pd.DataFrame:
    """Engine P(over) at the stored opening prop line, the line's no-vig, and the outcome."""
    pl = plines.assign(player_id=plines.player_id.astype(str))
    d = props.merge(pl[["game_id", "player_id", "market", "line", "over_price", "under_price"]],
                    on=["game_id", "player_id", "market", "line"])  # fmt: skip
    a = actual.assign(player_id=actual.player_id.astype(str))
    d = d.merge(a, on=["game_id", "player_id"], how="inner")
    stat = d.market.map(PROP_STATS)
    act = np.array([r[s] for (_, r), s in zip(d.iterrows(), stat, strict=True)], float)
    d["actual"] = act
    d = d[act != d.line.to_numpy()].copy()
    d["y"] = (d.actual > d.line).astype(float)
    d["p_eng"] = d.p_over / (1 - d.p_push).clip(lower=1e-9)
    d["p_mkt"] = novig(d.over_price, d.under_price)
    d["price_a"], d["price_b"] = d.over_price, d.under_price
    return d


def fit_design(gv: pd.DataFrame, pv: pd.DataFrame) -> dict:
    """Part C and E on validation: w and Platt per market, theta for game markets and props."""
    design = {"w": {}, "platt": {}, "blend_rows": {}, "calibration": {}, "theta": {},
              "validated": {}, "theta_rows": {}}  # fmt: skip
    fair = {}
    for name, df in [*((m, gv[gv.market == m]) for m in GAME_MARKETS),
                     *((m, pv[pv.market == m]) for m in PROP_MARKETS)]:
        if len(df) < 100:
            design["w"][name], design["platt"][name] = 0.0, None
            design["blend_rows"][name] = f"too few rows ({len(df)}): market only"
            continue
        w, rows = choose_weight(df.p_eng.to_numpy(), df.p_mkt.to_numpy(), df.y.to_numpy())
        f = w * df.p_eng.to_numpy() + (1 - w) * df.p_mkt.to_numpy()
        cal, info = calibrate(f, df.y.to_numpy())
        design["w"][name], design["platt"][name] = w, (list(cal) if cal else None)
        design["blend_rows"][name], design["calibration"][name] = rows, info
        fair[name] = apply_platt(f, cal)
    for fam, dfs in (("game", gv), ("prop", pv)):
        parts = [(m, dfs[dfs.market == m]) for m in (GAME_MARKETS if fam == "game"
                                                      else PROP_MARKETS) if m in fair]
        if not parts:
            design["theta"][fam], design["validated"][fam] = THETAS[-1], False
            continue
        ev, won, push, price = [], [], [], []
        for m, df in parts:
            e, side, pr = picks(fair[m], df.price_a.to_numpy(), df.price_b.to_numpy())
            ev.append(e)
            won.append(np.where(side == 0, df.y.to_numpy() == 1, df.y.to_numpy() == 0))
            push.append(np.zeros(len(df), bool))
            price.append(pr)
        th, ok, rows = choose_theta(np.concatenate(ev), np.concatenate(won),
                                    np.concatenate(push), np.concatenate(price))  # fmt: skip
        design["theta"][fam], design["validated"][fam], design["theta_rows"][fam] = th, ok, rows
    return design


def scored_picks(df: pd.DataFrame, design: dict, fam: str, close: np.ndarray | None = None):
    """The rows the pick rule picks, with fair, side, price, result and the 'close' no-vig of
    the picked side (`close`: market no-vig of side a at the reference close; default p_mkt)."""
    out = []
    for m in df.market.unique():
        d = df[df.market == m]
        w = design["w"].get(m, 0.0)
        cal = design["platt"].get(m)
        f = apply_platt(w * d.p_eng.to_numpy() + (1 - w) * d.p_mkt.to_numpy(),
                        tuple(cal) if cal else None)  # fmt: skip
        ev, side, price = picks(f, d.price_a.to_numpy(), d.price_b.to_numpy())
        sel = ev >= design["theta"][fam]
        c = d.p_mkt.to_numpy() if close is None else close[df.market.to_numpy() == m]
        x = d[sel].copy()
        x["fair_side"] = np.where(side == 0, f, 1 - f)[sel]
        x["eng_side"] = np.where(side == 0, d.p_eng, 1 - d.p_eng)[sel]
        x["close_side"] = np.where(side == 0, c, 1 - c)[sel]
        x["side_a"] = side[sel] == 0
        x["price"] = price[sel]
        x["win"] = np.where(side == 0, d.y == 1, d.y == 0)[sel]
        x["ev"] = ev[sel]
        out.append(x)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def checks_of(sp: pd.DataFrame) -> dict:
    from betdata.checks import Pick, three_checks

    ps = [Pick(float(r.fair_side), float(r.close_side), int(r.win), float(dec(r.price)))
          for r in sp.itertuples()]  # fmt: skip
    c = three_checks(ps)
    prof = np.where(sp.win, dec(sp.price) - 1, -1.0) if len(sp) else np.array([])
    return {"n": c.n, "status": c.status, "works": c.works,
            "wins": int(sp.win.sum()) if len(sp) else 0,
            "roi": float(prof.mean()) if len(prof) else None,
            "calibration": vars(c.calibration), "brier": vars(c.brier), "ev": vars(c.ev),
            "table": c.table}  # fmt: skip
