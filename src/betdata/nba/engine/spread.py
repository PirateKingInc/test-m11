"""The spread engine (owner 2026-11-14, MODEL.md "Spread engine"): the NBA engine's projected
point margin, recalibrated so it can judge spreads, and P(cover) at any line.

D1 (`diagnose`): why the engine's game probabilities are squeezed toward 50/50, measured on the
engine's own simulations of past seasons (per game: the simulated mean margin `mu`, its spread
`sd`, P(home win)) against the actual margins and the market's spread.
D2 (`SpreadModel`): margin_hat = a + b * mu (the engine's margin, de-shrunk and with home court
re-estimated), residual sigma; P(home covers line L | no push) = Phi((margin_hat + L) / sigma).
Fitted only on seasons before the scored one (`walk_forward`); the final test fits on
2016-2022 and scores 2023-2025 once (`test`).

Lines: closing spreads are the only pre-tip line stored for 2017-2022; openers exist for 2015-16
and 2023-24 on. The test scores the opening line (the earliest stored, before the morning). No
spread prices are stored for history: units are at -110.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

VALID = (2019, 2020, 2021, 2022)  # walk-forward validation (each fit on the seasons before)
TEST = (2023, 2024, 2025)
FIRST = 2016  # the engine's first simulated season (2015 seeds the ratings)
T_GRID = tuple(round(0.52 + 0.01 * i, 2) for i in range(11))  # 0.52 .. 0.62
WIN_AT_110 = 100 / 110


def _phi(x):
    from scipy.stats import norm

    return norm.cdf(x)


@dataclass
class SpreadModel:
    a: float
    b: float
    sigma: float
    seasons: tuple = ()

    @classmethod
    def fit(cls, d: pd.DataFrame) -> SpreadModel:
        """OLS of the actual margin on the engine's margin; sigma from the residuals."""
        x, y = d.mu.to_numpy(float), d.margin.to_numpy(float)
        b, a = np.polyfit(x, y, 1)
        sigma = float(np.std(y - (a + b * x), ddof=2))
        return cls(float(a), float(b), sigma, tuple(sorted(set(int(s) for s in d.season))))

    def margin(self, mu):
        return self.a + self.b * np.asarray(mu, float)

    def p_home_cover(self, mu, line):
        """P(home covers its spread `line` | no push): margin + line > 0."""
        return _phi((self.margin(mu) + np.asarray(line, float)) / self.sigma)

    def p_home_win(self, mu):
        return _phi(self.margin(mu) / self.sigma)


def _slope(x, y) -> float:
    return float(np.polyfit(np.asarray(x, float), np.asarray(y, float), 1)[0])


def _logit(p):
    p = np.clip(np.asarray(p, float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def novig_home(ml_home, ml_away):
    from betdata.nba.engine.evaluate import novig

    return novig(ml_home, ml_away)


def diagnose(d: pd.DataFrame) -> dict:
    """D1: the squeeze and its parts, on games with a closing spread and moneyline."""
    g = d.dropna(subset=["mu", "margin", "c_spread", "c_ml_home", "c_ml_away"]).copy()
    mkt = -g.c_spread  # the market's expected home margin
    pm = novig_home(g.c_ml_home, g.c_ml_away)
    resid = g.margin - g.mu
    sim_sd = float(g.sd.mean())
    res_sd = float(np.std(resid, ddof=1))
    fit = SpreadModel.fit(g)
    out = {
        "n": int(len(g)),
        # 1. the engine's margin is shrunk: actual on engine slope > 1, engine on market < 1
        "actual_on_engine_slope": round(_slope(g.mu, g.margin), 3),
        "engine_on_market_slope": round(_slope(mkt, g.mu), 3),
        "actual_on_market_slope": round(_slope(mkt, g.margin), 3),
        "sd_engine_margin": round(float(g.mu.std()), 2),
        "sd_market_margin": round(float(mkt.std()), 2),
        # 2. home court: the engine's mean margin vs the actual and the market's
        "mean_engine_margin": round(float(g.mu.mean()), 2),
        "mean_actual_margin": round(float(g.margin.mean()), 2),
        "mean_market_margin": round(float(mkt.mean()), 2),
        # 3. the margin-to-probability step: the simulations' spread vs the real residual
        "sim_sd_mean": round(sim_sd, 2), "residual_sd": round(res_sd, 2),
        # the h2h logit slope on the market's, raw and with each fix
        "h2h_logit_slope_raw": round(_slope(_logit(pm), _logit(g.p_home_win)), 3),
        "h2h_logit_slope_mean_fixed": round(_slope(_logit(pm), _logit(
            _phi(fit.margin(g.mu) / sim_sd))), 3),
        "h2h_logit_slope_sd_fixed": round(_slope(_logit(pm), _logit(
            _phi(g.mu / res_sd))), 3),
        "h2h_logit_slope_both_fixed": round(_slope(_logit(pm), _logit(
            fit.p_home_win(g.mu))), 3),
        "fit": {"a": round(fit.a, 3), "b": round(fit.b, 3), "sigma": round(fit.sigma, 2)},
        "engine_vs_market_slope_after": round(_slope(mkt, fit.margin(g.mu)), 3),
    }
    return out


def picks(d: pd.DataFrame, m: SpreadModel, line_col: str, t: float,
          dogs_only: bool = True) -> pd.DataFrame:  # fmt: skip
    """The side with P(cover) >= t at the line in `line_col` (the home spread), with its ATS
    result at -110; underdogs only by default (the side getting points)."""
    g = d.dropna(subset=[line_col, "mu", "margin"]).copy()
    line = g[line_col].to_numpy(float)
    ph = m.p_home_cover(g.mu, line)
    g["p_home"] = ph
    g["side"] = np.where(ph >= 0.5, "home", "away")
    g["p_side"] = np.where(ph >= 0.5, ph, 1 - ph)
    g["side_line"] = np.where(g.side == "home", line, -line)
    g = g[g.p_side >= t]
    if dogs_only:
        g = g[g.side_line > 0]
    cover = np.where(g.side == "home", g.margin + g[line_col], -(g.margin + g[line_col]))
    g["result"] = np.where(cover > 0, "win", np.where(cover < 0, "loss", "push"))
    g["units"] = np.where(g.result == "win", WIN_AT_110,
                          np.where(g.result == "loss", -1.0, 0.0))  # fmt: skip
    return g


def record(g: pd.DataFrame) -> dict:
    from betdata.nba.selection import wilson

    w, lo = int((g.result == "win").sum()), int((g.result == "loss").sum())
    p = int((g.result == "push").sum())
    ci = wilson(w, w + lo) if w + lo else None
    return {"n": int(len(g)), "w": w, "l": lo, "p": p,
            "win": None if not w + lo else round(w / (w + lo), 4),
            "win_95": None if ci is None else [round(ci[0], 4), round(ci[1], 4)],
            "units": round(float(g.units.sum()), 2),
            "roi": None if not len(g) else round(float(g.units.sum()) / len(g), 4)}


def walk_forward(d: pd.DataFrame, seasons=VALID) -> pd.DataFrame:
    """Each validation season scored by a model fitted on the seasons before it only (and from
    FIRST); the closing line (the only one stored for 2017-2022)."""
    out = []
    for s in seasons:
        m = SpreadModel.fit(d[(d.season >= FIRST) & (d.season < s)].dropna(
            subset=["mu", "margin"]))  # fmt: skip
        g = d[d.season == s].copy()
        g["_m"] = [m] * len(g)
        out.append(g)
    return pd.concat(out)


def choose_t(d: pd.DataFrame, min_picks: int = 150) -> tuple[float, list[dict]]:
    """Pre-registered on walk-forward validation (2019-2022, closing lines), underdogs only:
    the smallest T in T_GRID whose picks reach 53.5% with at least `min_picks`; if none does,
    the T with the highest win rate among those with at least `min_picks` picks."""
    v = walk_forward(d)
    table = []
    for t in T_GRID:
        gs = [picks(g, g["_m"].iloc[0], "c_spread", t) for _s, g in v.groupby("season")]
        r = record(pd.concat(gs)) if gs else record(pd.DataFrame(columns=["result", "units"]))
        table.append({"t": t, **r})
    ok = [r for r in table if r["n"] >= min_picks and (r["win"] or 0) >= 0.535]
    if ok:
        return ok[0]["t"], table
    big = [r for r in table if r["n"] >= min_picks] or table
    return max(big, key=lambda r: r["win"] or 0)["t"], table


def calibration_table(g: pd.DataFrame, m: SpreadModel, line_col: str) -> list[dict]:
    """P(home covers) in ten bins vs the actual cover rate (pushes dropped), with Brier vs the
    no-vig line (0.5 at a -110/-110 spread)."""
    g = g.dropna(subset=[line_col, "mu", "margin"])
    c = g.margin + g[line_col]
    g = g[c != 0]
    y = (g.margin + g[line_col] > 0).astype(float).to_numpy()
    p = m.p_home_cover(g.mu, g[line_col])
    bins = np.clip((p * 10).astype(int), 0, 9)
    rows = [{"bin": f"{b / 10:.1f}-{(b + 1) / 10:.1f}", "n": int((bins == b).sum()),
             "p_mean": round(float(p[bins == b].mean()), 4),
             "cover": round(float(y[bins == b].mean()), 4)}
            for b in range(10) if (bins == b).any()]  # fmt: skip
    return rows, {"brier_model": round(float(np.mean((p - y) ** 2)), 5),
                  "brier_line": round(float(np.mean((0.5 - y) ** 2)), 5), "n": int(len(y))}


def test(d: pd.DataFrame, t: float) -> dict:
    """D3: fitted once on 2016-2022, scored on 2023-2025 at the opening line."""
    m = SpreadModel.fit(d[(d.season >= FIRST) & (d.season <= 2022)].dropna(
        subset=["mu", "margin"]))  # fmt: skip
    te = d[d.season.isin(TEST)]
    cal, brier = calibration_table(te, m, "o_spread")
    dogs, alls = picks(te, m, "o_spread", t), picks(te, m, "o_spread", t, dogs_only=False)
    by = {int(s): {"dogs": record(dogs[dogs.season == s]),
                   "all": record(alls[alls.season == s])} for s in TEST}
    mkt = -te.dropna(subset=["o_spread"]).o_spread
    slope = _slope(mkt, m.margin(te.dropna(subset=["o_spread"]).mu))
    return {"model": {"a": m.a, "b": m.b, "sigma": m.sigma, "seasons": list(m.seasons)},
            "t": t, "line": "opening", "calibration": cal, "brier": brier,
            "dogs": record(dogs), "all": record(alls), "by_season": by,
            "engine_vs_market_slope": round(slope, 3)}


def bar(res: dict) -> dict:
    """D4: every condition on the test seasons, underdogs only, at T."""
    r = res["dogs"]
    pos = sum(1 for s in TEST if res["by_season"][s]["dogs"]["units"] > 0)
    checks = {
        "picks >= 300": (r["n"] >= 300, r["n"]),
        "win >= 53.5%": ((r["win"] or 0) >= 0.535, r["win"]),
        "95% low > 50%": (bool(r["win_95"]) and r["win_95"][0] > 0.5, r["win_95"]),
        "positive in >= 2 of 3 seasons": (pos >= 2, pos),
        "engine-vs-market slope 0.8-1.25": (0.8 <= res["engine_vs_market_slope"] <= 1.25,
                                            res["engine_vs_market_slope"]),
    }
    return {"met": all(v[0] for v in checks.values()),
            "checks": {k: {"ok": bool(v[0]), "value": v[1]} for k, v in checks.items()}}


def leakage_ok(m: SpreadModel) -> bool:
    return not set(m.seasons) & set(TEST)




# --- the data: the engine's simulations of past seasons ---------------------------------------


def build(conn, fit: dict, seasons, n_sims: int = 2000, log=None,
          point: str = "morning") -> pd.DataFrame:
    """Per final game of `seasons`: the engine's simulation at `point` (the morning, as a 6:15 AM
    pick would see it)
    with the constants in `fit` (mu = mean margin, sd, P(home win)), the actual margin, and
    the stored closing and opening home spread and closing moneylines."""
    from sqlalchemy import select

    from betdata.db.schema import results
    from betdata.nba.engine import evaluate as V
    from betdata.nba.engine import fitting as F
    from betdata.nba.engine.cli import _inputs
    from betdata.nba.engine.project import Fit

    inp = _inputs(conn)
    side = V.side_lines(conn, inp.g)
    eng = F.Engine(inp, Fit.from_json(fit))
    gids = inp.g[inp.g.season.isin(list(seasons)) & (inp.g.status == "final")].game_id
    import pickle
    from pathlib import Path

    raw = Path("data/derived/spread_engine_sims.pkl")  # kept, so a later step never re-simulates
    if raw.exists():
        games = pickle.loads(raw.read_bytes())
    else:
        games, _props, _d = V.simulate(eng, gids, None, side, n_sims, point=point, log=log)
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(pickle.dumps(games))
    res = pd.DataFrame(conn.execute(select(results.c.game_id, results.c.home_score,
                                           results.c.away_score)
                                    .where(results.c.final.is_(True))).all(),
                       columns=["game_id", "home_score", "away_score"])  # fmt: skip
    res = res.drop_duplicates("game_id")
    games = games.reset_index(drop=True) if "game_id" in games.columns else games.reset_index()
    side = side.copy()
    side.index = side.index.rename("_gid")
    g = (games.rename(columns={"mu_margin": "mu", "sd_margin": "sd"})
         [["game_id", "mu", "sd", "p_home_win"]]
         .merge(inp.g[["game_id", "season", "tip"]].reset_index(drop=True), on="game_id")
         .merge(res, on="game_id")
         .merge(side[["c_spread", "o_spread", "c_ml_home", "c_ml_away"]],
                left_on="game_id", right_index=True, how="left"))  # fmt: skip
    g["margin"] = g.home_score - g.away_score
    return g.sort_values(["tip", "game_id"]).reset_index(drop=True)


def d5(d: pd.DataFrame) -> dict:
    """D5 (recommendation only): would the D2 fix improve the engine's moneyline probability?
    On the test seasons, Brier and log loss of the raw engine P(home win), the fixed one
    (Phi(margin_hat / sigma), fitted on 2016-2022) and the closing no-vig moneyline."""
    m = SpreadModel.fit(d[(d.season >= FIRST) & (d.season <= 2022)].dropna(
        subset=["mu", "margin"]))  # fmt: skip
    g = d[d.season.isin(TEST)].dropna(subset=["c_ml_home", "c_ml_away", "mu"])
    g = g[g.margin != 0]
    y = (g.margin > 0).astype(float).to_numpy()
    out = {"n": int(len(y))}
    for name, p in (("raw_engine", g.p_home_win.to_numpy(float)),
                    ("fixed_engine", m.p_home_win(g.mu)),
                    ("market_close", novig_home(g.c_ml_home, g.c_ml_away))):  # fmt: skip
        p = np.clip(p, 1e-4, 1 - 1e-4)
        out[name] = {"brier": round(float(np.mean((p - y) ** 2)), 5),
                     "logloss": round(float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))),
                                      5)}  # fmt: skip
    out["h2h_logit_slope_raw"] = round(_slope(_logit(novig_home(g.c_ml_home, g.c_ml_away)),
                                              _logit(g.p_home_win)), 3)  # fmt: skip
    out["h2h_logit_slope_fixed"] = round(_slope(_logit(novig_home(g.c_ml_home, g.c_ml_away)),
                                                _logit(m.p_home_win(g.mu))), 3)  # fmt: skip
    return out


def _r(r: dict) -> str:
    if not r or not r.get("n"):
        return "0 picks"
    ci = r.get("win_95") or [None, None]
    lo = "–" if ci[0] is None else f"{100 * ci[0]:.1f}"
    hi = "–" if ci[1] is None else f"{100 * ci[1]:.1f}"
    win = "–" if r["win"] is None else f"{100 * r['win']:.1f}%"
    return (f"{r['w']}-{r['l']}" + (f"-{r['p']}" if r.get("p") else "")
            + f" ({win}, 95% {lo}-{hi}), {r['units']:+.1f}u at -110")


def report_md(valid: dict, test_res: dict | None, live: dict | None, when: str) -> str:
    dg = valid["diagnosis_2016_2022"]
    md = [f"# Spread engine ({when})", "",
          "Owner decision 2026-11-14 (MODEL.md \"Spread engine\"). Report only unless the D4 bar "
          "is met. The engine's constants are fitted on 2015-2022 only; every spread-model fit "
          "uses only seasons before the one it scores; T was chosen on walk-forward validation "
          "(2019-2022, closing lines) and recorded in MODEL.md before the test was run.", "",
          "## D1: why the engine's game probabilities are squeezed (2016-2022)", "",
          f"- Games: {dg['n']}.",
          f"- The engine's margin is shrunk: actual margin on the engine's margin, slope "
          f"**{dg['actual_on_engine_slope']}** (1 = right scale); the engine's margin on the "
          f"market's, slope **{dg['engine_on_market_slope']}** (the market's own: "
          f"{dg['actual_on_market_slope']}); spread of the engine's margins "
          f"{dg['sd_engine_margin']} pts vs the market's {dg['sd_market_margin']}.",
          f"- Home court: mean margin engine {dg['mean_engine_margin']:+.2f}, actual "
          f"{dg['mean_actual_margin']:+.2f}, market {dg['mean_market_margin']:+.2f}.",
          f"- Margin to probability: the simulations' margin spread {dg['sim_sd_mean']} pts per "
          f"game vs the actual residual {dg['residual_sd']} pts, so even a right mean would be "
          "pulled toward 50/50.",
          f"- The moneyline logit slope on the market's: raw **{dg['h2h_logit_slope_raw']}**; "
          f"mean fixed {dg['h2h_logit_slope_mean_fixed']}; spread fixed "
          f"{dg['h2h_logit_slope_sd_fixed']}; both fixed **{dg['h2h_logit_slope_both_fixed']}**.",
          f"- D2 fit on 2016-2022: margin = {dg['fit']['a']:+.2f} + {dg['fit']['b']:.2f} x "
          f"engine margin, sigma {dg['fit']['sigma']} pts; engine-vs-market slope after the fix "
          f"{dg['engine_vs_market_slope_after']}.", "",
          f"## D3 threshold (pre-registered): T = {valid['t']}", "",
          "| T | Validation underdog picks (walk-forward 2019-2022, closing line) |",
          "|---|---|"]
    md += [f"| {r['t']:.2f} | {_r(r)} |" for r in valid["validation_table"]]
    if test_res:
        b = test_res["brier"]
        md += ["", "## D3 test: 2023-24 to 2025-26 (opening lines; fitted once on 2016-2022)", "",
               f"- Brier of P(home covers): model {b['brier_model']} vs the line "
               f"(no-vig 0.5) {b['brier_line']} ({b['n']} games, pushes dropped).",
               f"- Underdogs at T: {_r(test_res['dogs'])}.",
               f"- All sides at T: {_r(test_res['all'])}.",
               f"- Engine-vs-market slope (fixed margin on the market's): "
               f"{test_res['engine_vs_market_slope']}.", "",
               "| Season | Underdogs | All sides |", "|---|---|---|"]
        md += [f"| {s}-{(s + 1) % 100:02d} | {_r(v['dogs'])} | {_r(v['all'])} |"
               for s, v in sorted(test_res["by_season"].items(), key=lambda kv: int(kv[0]))]
        md += ["", "Calibration (P(home covers) bins vs actual cover rate):", "",
               "| Bin | Games | Mean P | Covered |", "|---|---|---|---|"]
        md += [f"| {c['bin']} | {c['n']} | {c['p_mean']:.3f} | {c['cover']:.3f} |"
               for c in test_res["calibration"]]
        bar = test_res["bar"]
        md += ["", "## D4: the bar (test seasons, underdogs only, at T)", "",
               "**" + ("MET: Spread path 2 goes live" if bar["met"]
                       else "NOT MET: nothing ships live") + "**", ""]
        md += [f"- {k}: {'pass' if v['ok'] else 'FAIL'} ({v['value']})"
               for k, v in bar["checks"].items()]
        if "d5" in test_res:
            x = test_res["d5"]
            md += ["", "## D5: would the fix help Next Gen's moneyline probability? "
                   "(recommendation only)", "",
                   f"- Test seasons, {x['n']} games: Brier raw engine "
                   f"{x['raw_engine']['brier']}, fixed {x['fixed_engine']['brier']}, closing "
                   f"market {x['market_close']['brier']}; log loss "
                   f"{x['raw_engine']['logloss']} / {x['fixed_engine']['logloss']} / "
                   f"{x['market_close']['logloss']}; logit slope on the market's raw "
                   f"{x['h2h_logit_slope_raw']}, fixed {x['h2h_logit_slope_fixed']}."]
    if live:
        md += ["", "## Live season (labelled)", "", *live.get("lines", [])]
    return "\n".join(md) + "\n"


def live_section(grades: list[dict], t: float) -> dict:
    """The live season, labelled: engine_grades.csv stores the home side of each spread at the
    close with the raw engine's probability (the fixed model needs the engine's mean margin,
    stored from Nov 15 in engine_game_grades.csv with both sides). Underdog picks at T by the
    RAW engine, as a reference for what the squeeze does."""
    rows = [r for r in grades if r.get("kind") == "game" and r.get("market") == "spreads"
            and r.get("p_engine") and r.get("line") not in ("", None)]  # fmt: skip
    res = []
    for r in rows:
        ln, ph = float(r["line"]), float(r["p_engine"])
        side, p = ("home", ph) if ln > 0 else ("away", 1 - ph)
        if ln == 0 or p < t:
            continue
        hr = r.get("result")
        out = hr if side == "home" else {"win": "loss", "loss": "win"}.get(hr, hr)
        res.append({"result": out, "units": WIN_AT_110 if out == "win" else
                    (-1.0 if out == "loss" else 0.0)})  # fmt: skip
    g = pd.DataFrame(res, columns=["result", "units"])
    rec = record(g)
    dogs_all = sum(1 for r in rows if float(r["line"]) != 0)
    return {"record": rec, "lines": [
        f"- Raw engine (before the fix), home-side closing spreads in engine_grades.csv: "
        f"{len(rows)} games; the raw engine takes the underdog at P >= {t} in "
        f"{rec['n']} of {dogs_all}: {_r(rec)}.",
        "- The fixed spread model's live inputs (the engine's mean margin, both sides, the "
        "morning line) are stored from the Nov 15 morning run (engine_game_grades.csv); its "
        "live record starts then."]}
