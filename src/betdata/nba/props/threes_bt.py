"""Threes under today's OG rules, and today's rules on the old seasons (owner research request
2026-11-12; reports/nba/threes_backtest.md). RESEARCH ONLY: nothing here touches a live card,
pick, model, filter, ranking, alert, push, page or ledger file.

The rule under test is OG Ranking v2 (MODEL.md), per market, fixed before looking:
- **candidate:** nbap15's edge at the opening line and price, P(side | no push) minus the book's
  no-vig, of at least the market's threshold: F's live 6 pp for rebounds and assists
  (alerts/snipe.py EDGE); threes has no live F threshold, so M15's validated 8 pp
  (reports/nba/props_tune.json). F's consensus check ("other books do not side against") cannot
  apply: the history keeps one book per line.
- price -150 to +130 inclusive; nbap15's median (of its negative binomial at the projection, the
  `median` live F stores) on the bet's side of the line (over: > line, under: < line);
- one pick per player-game-market (the history has one line per player-game-market);
- rank by the model probability of the side; ties: the shorter price; top N per card day.
Model and probabilities: nbap15 exactly as in its final test (fit on 2015-16 to 2022-23 with the
validated calibration); nothing is fitted or chosen on 2023-24 to 2025-26. The F-style Platt
check is fitted walk-forward (only seasons before the scored one).

Rules v2 on the old seasons (assists and rebounds): (i) unders on assist lines >= 5.5;
(ii) unders with 2+ key teammates (>= 24 min over their last 10 games before the prediction
point) Out or Doubtful on the NBA injury report known at the prediction point (`review` helpers:
the live rule's own code, as of the point); (iii) rebounds as a whole.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

MARKETS = ("threes", "assists", "rebounds")
THRESHOLD = {"rebounds": 6.0, "assists": 6.0, "threes": 8.0}  # pp: F live, M15 for threes
PRICE_MIN, PRICE_MAX = -150, 130
TEST_SEASONS = (2023, 2024, 2025)
POINTS = ("pretip", "morning")
REPORT_MD = Path("reports/nba/threes_backtest.md")
REPORT_JSON = Path("reports/nba/threes_backtest.json")
UNDER_LINE = 5.5
TEAMMATES_MAX = 2
STARTER_MIN = 28.0  # projected minutes for "starter" in the threes breakdown


# --- the rule ----------------------------------------------------------------------------------


def candidates(d: pd.DataFrame, market: str, threshold: float | None = None) -> pd.DataFrame:
    """F-style candidates from priced rows (select.priced + `median`, `m_hat`): the side whose
    model probability beats the book's no-vig by the threshold, with its price and result."""
    from betdata.moneyline import profit

    th = (THRESHOLD[market] if threshold is None else threshold) / 100.0
    over = (d.p - d.nv) >= th
    under = ((1 - d.p) - (1 - d.nv)) >= th
    c = d[over | under].copy()
    c["market"] = market
    c["side"] = np.where(over[over | under], "over", "under")
    c["ps"] = np.where(c.side == "over", c.p, 1 - c.p)
    c["nv_side"] = np.where(c.side == "over", c.nv, 1 - c.nv)
    c["price"] = np.where(c.side == "over", c.over_price, c.under_price).astype(int)
    c["result"] = np.where(c.over.isna(), "push",
                           np.where((c.over == 1) == (c.side == "over"), "win", "loss"))
    c["profit"] = [profit(r, int(p)) for r, p in zip(c.result, c.price, strict=True)]
    return c


def ranking_v2(c: pd.DataFrame) -> pd.DataFrame:
    """OG Ranking v2's price and median rules, then one pick per player-game-market (the
    highest probability), sorted by probability (ties: the shorter price)."""
    from betdata.alerts.rules import decimal

    ok_price = c.price.between(PRICE_MIN, PRICE_MAX)
    ok_med = np.where(c.side == "over", c["median"] > c.line, c["median"] < c.line)
    e = c[ok_price & ok_med].copy()
    e["dec"] = [decimal(int(x)) for x in e.price]
    e = e.sort_values(["ps", "dec"], ascending=[False, True])
    e = e.drop_duplicates(["game_id", "player_id", "market"])
    return e


def top_per_day(e: pd.DataFrame, n: int) -> pd.DataFrame:
    """The top n per card day by probability (ties: the shorter price), from a ranked pool."""
    e = e.sort_values(["day", "ps", "dec"], ascending=[True, False, True])
    return e.groupby("day", group_keys=False).head(n)


# --- records -----------------------------------------------------------------------------------


def american(dec: float) -> int:
    return round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))


def rec(b: pd.DataFrame, days: int | None = None) -> dict:
    from betdata.nba.selection import wilson

    w, lo = int((b.result == "win").sum()), int((b.result == "loss").sum())
    p = int((b.result == "push").sum())
    n = len(b)
    ci = wilson(w, w + lo) if w + lo else None
    dec = b.dec.mean() if "dec" in b and n else None
    return {"picks": n, "w": w, "l": lo, "p": p,
            "win": None if not w + lo else round(w / (w + lo), 4),
            "win_95": None if ci is None else [round(ci[0], 4), round(ci[1], 4)],
            "units": round(float(b.profit.sum()), 2), "roi": None if not n else
            round(float(b.profit.sum()) / n, 4),
            "avg_odds": None if dec is None or dec != dec else american(float(dec)),
            "per_day": None if not days else round(n / days, 2)}  # fmt: skip


def wl(r: dict) -> str:
    return f"{r['w']}-{r['l']}" + (f"-{r['p']}" if r.get("p") else "")


# --- the data ----------------------------------------------------------------------------------


def nb_median(disp, mu: np.ndarray) -> np.ndarray:
    """nbap15's median at each projection (the `median` live F stores, props/dist.quantiles)."""
    from scipy.stats import nbinom

    from betdata.props.dist import nb_params

    out = np.empty(len(mu))
    d = getattr(disp, "disp", disp)
    for i, m in enumerate(mu):
        n, p = nb_params(d, float(m))
        out[i] = nbinom.ppf(0.5, float(n), float(p))
    return out


def load(conn, seasons=(2022, *TEST_SEASONS)) -> dict:
    """{(market, point): priced rows with median, projected minutes, day and season} at the
    opening line, for 2022-23 (validation: walk-forward fits only) and the test seasons."""
    from betdata.nba.pipeline import TRAIN, VALIDATE
    from betdata.nba.props import model as M
    from betdata.nba.props import select as S

    tune = json.loads((S.REPORTS / "props_tune.json").read_text())
    f = S.usable(S.history_features())
    mod = M.fit(f[f.season.isin((*TRAIN, *VALIDATE))])  # nbap15's final-test fit
    for mk, r in tune["markets"].items():
        if r.get("calibration_applied"):
            mod.calib[mk] = tuple(r["platt"])
    lines = S.history_lines(conn)
    tips = pd.read_sql("select game_id, start_time_utc from games", conn)
    tips["day"] = (pd.to_datetime(tips.start_time_utc, utc=True)
                   .dt.tz_convert("America/New_York").dt.date.astype(str))  # fmt: skip
    tips = tips.set_index("game_id")
    out = {}
    te = f[f.season.isin(seasons)]
    for mk in MARKETS:
        for pt in POINTS:
            d = S.priced(mod, te[te.point == pt], lines, mk, S.LINE)
            if d.empty:
                continue
            d["m_hat"] = mod.predict_minutes(d)
            d["median"] = nb_median(mod.disp[mk], d.mu.to_numpy())
            d["day"] = d.game_id.map(tips.day)
            d["tip"] = d.game_id.map(tips.start_time_utc)
            out[(mk, pt)] = d
    return {"pools": out, "model_calib": {k: list(v) for k, v in mod.calib.items()}}


def names(conn) -> dict:
    """player_id -> latest box-score name (the live rule matches by name)."""
    r = pd.read_sql("select player_id, player_name from nba_player_games", conn)
    return dict(zip(r.player_id.astype(str), r.player_name, strict=False))


def teammates_out(engine, rows: pd.DataFrame, name_of: dict) -> list:
    """The live Rules v2 helper (eligibility.teammates_out) at each row's prediction point:
    key teammates Out/Doubtful on the injury report known then; None when no report was known."""
    from betdata import eligibility as EL

    out = []
    with engine.connect() as m:
        for r in rows.itertuples():
            at = pd.Timestamp(r.t).to_pydatetime()
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            try:
                out.append(EL.teammates_out(m, int(r.game_id), int(r.team_id),
                                            name_of.get(str(r.player_id)), at))  # fmt: skip
            except Exception:  # noqa: BLE001 - unknown: the rule cannot apply
                out.append(None)
    return out


# --- the walk-forward Platt check ----------------------------------------------------------------


def platt_walkforward(cands: pd.DataFrame) -> dict:
    """An F-style global Platt fit of the side's result on logit(ps), fitted per scored season
    on the earlier seasons' candidates only; returns the fits and whether the ranking is
    unchanged (it must be: the fit is increasing)."""
    from betdata.calibration import fit

    out = {}
    for s in TEST_SEASONS:
        tr = cands[(cands.season < s) & cands.result.isin(("win", "loss"))]
        if len(tr) < 200:
            out[s] = {"n": int(len(tr)), "fit": None}
            continue
        a, b = fit(tr.ps.tolist(), (tr.result == "win").astype(int).tolist())
        out[s] = {"n": int(len(tr)), "slope": round(a, 4), "intercept": round(b, 4)}
    return out


def calibrated(ps: np.ndarray, a: float, b: float) -> np.ndarray:
    p = np.clip(ps, 1e-6, 1 - 1e-6)
    return 1 / (1 + np.exp(-(a * np.log(p / (1 - p)) + b)))


# --- the analysis ------------------------------------------------------------------------------


def pool_checks(d: pd.DataFrame) -> dict:
    """Calibration of P(over), Brier vs the opening no-vig, over every priced row."""
    from betdata.nba.props import select as S

    ok = d.over.notna()
    p, y, nv = d.p[ok].to_numpy(), d.over[ok].to_numpy(), d.nv[ok].to_numpy()
    return {"rows": int(ok.sum()), "brier_model": round(float(np.mean((p - y) ** 2)), 5),
            "brier_novig": round(float(np.mean((nv - y) ** 2)), 5),
            "calibration": S.calibration(p, y)}  # fmt: skip


def ev_open(b: pd.DataFrame) -> float | None:
    """Mean EV at the opening no-vig of the side (no true close for most rows)."""
    if b.empty:
        return None
    from betdata.alerts.rules import decimal

    return round(float(np.mean([q * decimal(int(x)) - 1 for q, x in
                                zip(b.nv_side, b.price, strict=True)])), 4)  # fmt: skip


def breakdowns(b: pd.DataFrame) -> dict:
    def line_bucket(x):
        return "3.5+" if x >= 3.5 else f"{x:g}"

    def prob_bucket(x):
        return "60+" if x >= 0.60 else "55-60" if x >= 0.55 else "50-55" if x >= 0.50 \
            else "<50"

    keys = {"side": b.side, "line": b.line.map(line_bucket),
            "money": np.where(b.price > 0, "plus", "minus"), "prob": b.ps.map(prob_bucket),
            "role": np.where(b.m_hat >= STARTER_MIN, "starter-level", "bench-level")}
    return {k: {str(g): rec(x) for g, x in b.groupby(v)} for k, v in keys.items()}


def analyse(pools: dict, tm: dict | None = None) -> dict:
    """Every table of the report, from load()'s pools (and the teammates-out counts, keyed
    (market, point, game_id, player_id), from teammates_out)."""
    res: dict = {"markets": {}, "cards": {}, "rules_v2": {}, "platt": {}}
    ranked: dict = {}
    for (mk, pt), d in pools.items():
        c = candidates(d, mk)
        e = ranking_v2(c)
        test = e[e.season.isin(TEST_SEASONS)]
        days = {s: d[d.season == s].day.nunique() for s in TEST_SEASONS}
        alld = sum(days.values())
        ranked[(mk, pt)] = e
        r = {"pool_checks": pool_checks(d[d.season.isin(TEST_SEASONS)]),
             "candidates": rec(c[c.season.isin(TEST_SEASONS)].assign(
                 dec=[1 + (x / 100 if x > 0 else 100 / -x) for x in
                      c[c.season.isin(TEST_SEASONS)].price])),
             "ranking_v2_pool": rec(test, alld), "ev_open_pool": ev_open(test)}
        for n in (3, 5):
            t = top_per_day(test, n)
            r[f"top{n}"] = {"all": rec(t, alld), "ev_open": ev_open(t),
                            "by_season": {int(s): rec(t[t.season == s], days[s])
                                          for s in TEST_SEASONS}}  # fmt: skip
            if mk == "threes":
                r[f"top{n}"]["breakdowns"] = breakdowns(t)
        if mk == "threes":
            r["pool_breakdowns"] = breakdowns(test)
        wf = platt_walkforward(c)
        cal_exp = {}
        for s, x in wf.items():
            t5 = top_per_day(test, 5)
            t5 = t5[t5.season == s]
            if x.get("slope") and len(t5):
                pc = calibrated(t5.ps.to_numpy(), x["slope"], x["intercept"])
                order_same = bool((np.argsort(-pc, kind="stable")
                                   == np.argsort(-t5.ps.to_numpy(), kind="stable")).all())
                cal_exp[int(s)] = {**x, "expected_win": round(float(pc.mean()), 4),
                                   "raw_mean_p": round(float(t5.ps.mean()), 4),
                                   "actual_win": rec(t5)["win"],
                                   "ranking_unchanged": order_same and x["slope"] > 0}
            else:
                cal_exp[int(s)] = x
        r["platt_walkforward_top5"] = cal_exp
        res["markets"].setdefault(mk, {})[pt] = r
    # the combined assists + threes card, and the overlap
    for pt in POINTS:
        a, t = ranked.get(("assists", pt)), ranked.get(("threes", pt))
        if a is None or t is None:
            continue
        days = pools[("assists", pt)].loc[lambda x: x.season.isin(TEST_SEASONS)].day.nunique()
        both = pd.concat([a, t])
        both = both[both.season.isin(TEST_SEASONS)]
        out = {}
        for n in (3, 5):
            cb = top_per_day(both, n)
            ao = top_per_day(a[a.season.isin(TEST_SEASONS)], n)
            out[f"top{n}"] = {"assists_alone": rec(ao, days), "assists_plus_threes": rec(cb, days),
                              "threes_share": round(float((cb.market == "threes").mean()), 3)
                              if len(cb) else None}  # fmt: skip
            pairs = cb.groupby(["game_id", "player_id"]).market.nunique()
            pg = set(pairs[pairs > 1].index)
            j = cb[[k in pg for k in zip(cb.game_id, cb.player_id, strict=True)]]
            joint = j.groupby(["game_id", "player_id"]).result.apply(
                lambda r: "both won" if (r == "win").all() else
                "both lost" if (r == "loss").all() else "split")  # fmt: skip
            out[f"top{n}"]["same_player_game"] = {"pairs": len(pg),
                                                  **joint.value_counts().to_dict()}
        res["cards"][pt] = out
    # Rules v2 on the old seasons and the full card three ways
    for pt in POINTS:
        res["rules_v2"][pt] = rules_v2(ranked, pools, pt, tm)
    return res


def rules_v2(ranked: dict, pools: dict, pt: str, tm: dict | None) -> dict:
    a = ranked.get(("assists", pt))
    rb = ranked.get(("rebounds", pt))
    th = ranked.get(("threes", pt))
    if a is None or rb is None:
        return {}
    a = a[a.season.isin(TEST_SEASONS)].copy()
    rb = rb[rb.season.isin(TEST_SEASONS)].copy()
    days = pools[("assists", pt)].loc[lambda x: x.season.isin(TEST_SEASONS)].day.nunique()
    out: dict = {}
    u55 = (a.side == "under") & (a.line >= UNDER_LINE - 1e-9)
    out["i_under_5_5_assists"] = {"removed": rec(a[u55]), "kept": rec(a[~u55]),
                                  "by_season_removed": {int(s): rec(g) for s, g in
                                                        a[u55].groupby("season")}}  # fmt: skip
    for name, x in (("assists", a), ("rebounds", rb)):
        if tm is None:
            continue
        x["tm"] = [tm.get((name, pt, g, p)) for g, p in zip(x.game_id, x.player_id,
                                                             strict=True)]  # fmt: skip
        un = x[x.side == "under"]
        known = un[un.tm.notna()]
        hit = known[known.tm >= TEAMMATES_MAX]
        out[f"ii_teammates_out_{name}"] = {
            "unders": int(len(un)), "report_known": int(len(known)),
            "coverage_by_season": {int(s): round(float(g.tm.notna().mean()), 3)
                                   for s, g in un.groupby("season")},
            "removed": rec(hit), "kept": rec(known[known.tm < TEAMMATES_MAX])}  # fmt: skip
        if name == "assists":
            a = x
    out["iii_rebounds_ranking_v2"] = {"pool": rec(rb), "by_season": {
        int(s): rec(g) for s, g in rb.groupby("season")}}

    def v2_mask(x):
        m = ~((x.side == "under") & (x.line >= UNDER_LINE - 1e-9))
        if "tm" in x:
            m &= ~((x.side == "under") & (x.tm.fillna(0) >= TEAMMATES_MAX))
        return m

    base = pd.concat([a, rb])
    kept = a[v2_mask(a)]
    three = th[th.season.isin(TEST_SEASONS)] if th is not None else None
    for n in (3, 5):
        out[f"card_top{n}"] = {
            "ranking_v2": rec(top_per_day(base, n), days),
            "ranking_v2_plus_rules_v2": rec(top_per_day(kept, n), days),
            "plus_threes": None if three is None else
            rec(top_per_day(pd.concat([kept, three]), n), days)}  # fmt: skip
    return out


def verdict(removed: dict, kept: dict) -> str:
    """Did a filter hold up: the removed picks lost money and did worse than the kept ones."""
    if removed["w"] + removed["l"] < 30:
        return "too few picks to tell"
    if (removed["roi"] or 0) < 0 and (removed["win"] or 0) <= (kept["win"] or 0):
        return "held up on old seasons"
    return "did not hold up"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def nan_to_none(x):
    if isinstance(x, float) and math.isnan(x):
        return None
    return x


# --- replication on the live days ---------------------------------------------------------------


def replicate_live(out: Path) -> dict:
    """This module's rule vs the live OG Ranking v2 code (og_ranking.order, through the
    walk-forward tool's ledger pool) on every OG card day: the same top 5, day by day. The live
    pool is F / C-NBA picks already past F's own edge rule, after Rules v2 and Ranking v2's
    price / median rules (walkforward.pool)."""
    from betdata import og_ranking as OR
    from betdata import walkforward as WF

    data = WF.load(out)
    days = {}
    for day, t in data.days.items():
        cands, _, _ = WF.pool(data, day, t)
        live, _ = OR.order(cands, None)
        live_ids = [a.dedup_key for a in live[:5]]
        if not cands:
            days[day] = {"live": [], "replica": [], "same": True}
            continue
        df = pd.DataFrame({"pick_id": [a.dedup_key for a in cands],
                           "game_id": [a.game_id for a in cands],
                           "player_id": [(a.player_name or "").lower() for a in cands],
                           "market": [a.market for a in cands],
                           "side": [a.side for a in cands], "line": [a.line for a in cands],
                           "price": [int(a.price) for a in cands],
                           "median": [a.details.get("median") for a in cands],
                           "ps": [float(a.fair_prob) for a in cands],
                           "created": [a.created_at for a in cands], "day": day})
        # the live dedup is one pick per player and game (OG is assists only after Rules v2,
        # so per player-game-market is the same); the live tie-break adds created_at
        e = ranking_v2(df.sort_values("created"))
        rep = list(top_per_day(e, 5).pick_id)
        days[day] = {"live": live_ids, "replica": rep, "same": live_ids == rep}
    return {"days": days, "same": sum(d["same"] for d in days.values()), "of": len(days)}


# --- the write-up ------------------------------------------------------------------------------


def _row(name: str, r: dict | None) -> str:
    if not r:
        return f"| {name} | – | – | – | – | – | – | – |"
    w95 = "–" if not r.get("win_95") else f"{100 * r['win_95'][0]:.1f}-{100 * r['win_95'][1]:.1f}"
    odds = "–" if r.get("avg_odds") is None else f"{r['avg_odds']:+d}"
    roi = "–" if r.get("roi") is None else f"{100 * r['roi']:+.1f}%"
    win = "–" if r.get("win") is None else f"{100 * r['win']:.1f}%"
    pd_ = "–" if r.get("per_day") is None else f"{r['per_day']:.2f}"
    return (f"| {name} | {r['picks']} | {wl(r)} | {win} ({w95}) | {r['units']:+.1f}u | {roi} | "
            f"{odds} | {pd_} |")


def _short(r: dict) -> str:
    return (f"{wl(r)} ({100 * r['win']:.1f}%, {100 * r['roi']:+.1f}% ROI, {r['per_day']:.1f} a "
            "day)")


HEAD = ["| | Picks | W-L-P | Win % (95%) | Units (1u) | ROI | Avg odds | Picks/day |",
        "|---|---|---|---|---|---|---|---|"]


def report(R: dict, extra: dict) -> str:
    M, C2 = R["markets"], R["rules_v2"]
    t = M["threes"]["pretip"]
    c2 = C2["pretip"]
    v_i = verdict(c2["i_under_5_5_assists"]["removed"], c2["i_under_5_5_assists"]["kept"])
    v_ii = verdict(c2["ii_teammates_out_assists"]["removed"],
                   c2["ii_teammates_out_assists"]["kept"])
    rb = c2["iii_rebounds_ranking_v2"]["pool"]
    v_iii = ("held up on old seasons" if (rb["roi"] or 0) < 0 else "did not hold up")
    live = extra["live"]
    av = extra["availability"]
    cards = R["cards"]["pretip"]
    md = ["# Threes under today's OG rules (research; hindsight backtest)", "",
          f"Written {now_iso()[:10]} (owner research request 2026-11-12). **RESEARCH ONLY: no "
          "live card, pick, model, filter, ranking, alert, push, page or ledger file changed.** "
          "Every number below is a backtest on 2023-24, 2024-25 and 2025-26 (nothing fitted or "
          "chosen on them), at ESPN's stored opening line and price, except the live-ledger "
          "comparison, which is labelled. Main figures are at the pretip prediction point; "
          "morning is in the tables.", "",
          "## Plain-English answers", "",
          f"1. **Would threes picks chosen by today's OG rules have won?** Modestly: top 5 a day "
          f"{wl(t['top5']['all'])} ({100 * t['top5']['all']['win']:.1f}%, "
          f"{100 * t['top5']['all']['roi']:+.1f}% ROI, {t['top5']['all']['per_day']:.1f} a day); "
          f"top 3 {wl(t['top3']['all'])} ({100 * t['top3']['all']['win']:.1f}%, "
          f"{100 * t['top3']['all']['roi']:+.1f}%). Almost all are unders "
          f"({t['top5']['breakdowns']['side'].get('under', {}).get('picks', 0)} of "
          f"{t['top5']['all']['picks']}); 0.5 lines and bench-level shooters lost, 3.5+ lines and "
          "starter-level shooters carried it. The model is worse than the no-vig opening line "
          f"on Brier over every priced row ({M['threes']['pretip']['pool_checks']['brier_model']} "
          f"vs {M['threes']['pretip']['pool_checks']['brier_novig']}), so the edge is narrow.",
          f"2. **Does adding threes help the card?** Top 5: assists alone "
          f"{wl(cards['top5']['assists_alone'])} ({100 * cards['top5']['assists_alone']['win']:.1f}%, "  # noqa: E501
          f"{100 * cards['top5']['assists_alone']['roi']:+.1f}%, "
          f"{cards['top5']['assists_alone']['per_day']:.1f}/day) vs assists + threes "
          f"{wl(cards['top5']['assists_plus_threes'])} "
          f"({100 * cards['top5']['assists_plus_threes']['win']:.1f}%, "
          f"{100 * cards['top5']['assists_plus_threes']['roi']:+.1f}%, "
          f"{cards['top5']['assists_plus_threes']['per_day']:.1f}/day): higher win rate and more "
          "picks a day. Top 3: "
          f"{100 * cards['top3']['assists_alone']['win']:.1f}% "
          f"({100 * cards['top3']['assists_alone']['roi']:+.1f}%) vs "
          f"{100 * cards['top3']['assists_plus_threes']['win']:.1f}% "
          f"({100 * cards['top3']['assists_plus_threes']['roi']:+.1f}%): a higher win rate but a "
          "lower ROI (threes take the slots at shorter prices). On the full card with Rules v2, "
          "adding threes restores most of what Rules v2 removed (table below).",
          f"3. **Rules v2 on the old seasons:** (i) unders on assist lines 5.5+ — **{v_i}**: the "
          f"removed picks went {wl(c2['i_under_5_5_assists']['removed'])} "
          f"({100 * c2['i_under_5_5_assists']['removed']['roi']:+.1f}% ROI), better than the kept "
          f"ones ({100 * c2['i_under_5_5_assists']['kept']['roi']:+.1f}%), though the edge shrank "
          "every season (by season below). (ii) assist unders with 2+ key teammates out — "
          f"**{v_ii}**: removed {wl(c2['ii_teammates_out_assists']['removed'])} "
          f"({100 * c2['ii_teammates_out_assists']['removed']['roi']:+.1f}%) vs kept "
          f"{100 * c2['ii_teammates_out_assists']['kept']['roi']:+.1f}%. (iii) rebounds — "
          f"**{v_iii}**: rebounds under Ranking v2 went {wl(rb)} ({100 * rb['win']:.1f}%, "
          f"{100 * rb['roi']:+.1f}%) on old seasons, but live F rebounds went "
          f"{live['rebounds']['wl']} ({100 * live['rebounds']['win']:.1f}%, "
          f"{100 * live['rebounds']['roi']:+.1f}%).",
          f"4. **How far to trust a backtest:** live F rebounds since Oct 29 trail this "
          f"pipeline's F-style rebounds candidates by {extra['gap']['rebounds']['win_pp']:+.1f} pp "
          f"of win rate and {extra['gap']['rebounds']['roi_pp']:+.1f} pp of ROI (and the M15-rule "
          "backtest's +10.8% ROI by far more); live assists ran better than the backtest "
          f"({extra['gap']['assists']['win_pp']:+.1f} pp, {extra['gap']['assists']['roi_pp']:+.1f} pp). "  # noqa: E501
          f"A rebounds-sized gap would turn threes' {100 * t['top5']['all']['roi']:+.1f}% into "
          f"about {100 * t['top5']['all']['roi'] + extra['gap']['rebounds']['roi_pp']:+.1f}%: at "
          "best a small edge, at worst a loss. Samples are small (live: a few hundred picks).",
          f"5. **Live lines:** yes. SharpAPI's free plan already delivers DK and FD made-threes "
          f"props and the poller stores them (about {av['sharpapi']['players_per_game']} players a "
          f"game per book, first seen a median {av['sharpapi']['first_seen_h']} h before tip, the "
          "same sweep as rebounds and assists); The Odds API's `player_threes` also quotes DK and "
          f"FD ({av['odds_api']['credits_used']} credits used for this check). Live box scores "
          "store fg3m for grading. Threes could go live without a new source.",
          "6. **The full card three ways (top 5 a day, pretip):** Ranking v2 on assists + "
          f"rebounds {_short(C2['pretip']['card_top5']['ranking_v2'])}; + Rules v2 (today's OG) "
          f"{_short(C2['pretip']['card_top5']['ranking_v2_plus_rules_v2'])}; + Rules v2 + threes "
          f"{_short(C2['pretip']['card_top5']['plus_threes'])}. On the old seasons Rules v2 "
          "cost win rate and units, and threes put most of it back.", "",
          "## Threes (top 3 / top 5 a day, by season)", "", *HEAD]
    for pt in POINTS:
        x = M["threes"][pt]
        for n in (3, 5):
            md.append(_row(f"{pt} top {n}, all", x[f"top{n}"]["all"]))
            for s in TEST_SEASONS:
                md.append(_row(f"{pt} top {n}, {s}-{str(s + 1)[2:]}", x[f"top{n}"]["by_season"][s]))
        md.append(_row(f"{pt} every Ranking v2 pick", x["ranking_v2_pool"]))
        md.append(_row(f"{pt} every F-style candidate (before Ranking v2)", x["candidates"]))
    md += ["", "### Threes top 5 (pretip) breakdowns", "", *HEAD]
    for k, groups in t["top5"]["breakdowns"].items():
        for g, r in groups.items():
            md.append(_row(f"{k}: {g}", r))
    md += ["", "Starter-level = projected minutes >= 28 (nbap15's minutes model).", "",
           "## Assists and rebounds under Ranking v2 (no Rules v2)", "", *HEAD]
    for mk in ("assists", "rebounds"):
        for pt in POINTS:
            for n in (3, 5):
                md.append(_row(f"{mk} {pt} top {n}", M[mk][pt][f"top{n}"]["all"]))
    md += ["", "## The combined assists + threes card (Ranking v2, no Rules v2)", "", *HEAD]
    for pt in POINTS:
        for n in (3, 5):
            c = R["cards"][pt][f"top{n}"]
            md.append(_row(f"{pt} top {n}: assists alone", c["assists_alone"]))
            md.append(_row(f"{pt} top {n}: assists + threes (threes {100 * c['threes_share']:.0f}% "
                           "of picks)", c["assists_plus_threes"]))
            sp = c["same_player_game"]
            md.append(f"| {pt} top {n}: same player-game in both markets | {sp['pairs']} pairs: "
                      f"both won {sp.get('both won', 0)}, both lost {sp.get('both lost', 0)}, "
                      f"split {sp.get('split', 0)} | | | | | | |")
    md += ["", "## Rules v2 on the old seasons (assists and rebounds)", "", *HEAD]
    for pt in POINTS:
        c = C2[pt]
        md += [_row(f"{pt} (i) assist unders 5.5+: removed", c["i_under_5_5_assists"]["removed"]),
               _row(f"{pt} (i) every other assists pick: kept", c["i_under_5_5_assists"]["kept"])]
        for s, r in c["i_under_5_5_assists"]["by_season_removed"].items():
            md.append(_row(f"{pt} (i) removed, {s}-{str(s + 1)[2:]}", r))
        for mk in ("assists", "rebounds"):
            x = c.get(f"ii_teammates_out_{mk}")
            if x:
                md += [_row(f"{pt} (ii) {mk} unders, 2+ key teammates out: removed", x["removed"]),
                       _row(f"{pt} (ii) {mk} unders, fewer: kept", x["kept"])]
        md.append(_row(f"{pt} (iii) rebounds under Ranking v2", c["iii_rebounds_ranking_v2"]["pool"]))  # noqa: E501
        for s, r in c["iii_rebounds_ranking_v2"]["by_season"].items():
            md.append(_row(f"{pt} (iii) rebounds, {s}-{str(s + 1)[2:]}", r))
    md += ["", "(ii) reads each team's key players (>= 24 min over their last 10 games before the "
           "prediction point) and the injury report known at the point, with the live rule's own "
           "code; a report was known for "
           f"{100 * c2['ii_teammates_out_assists']['report_known'] / max(1, c2['ii_teammates_out_assists']['unders']):.1f}% "  # noqa: E501
           "of assist unders (no season skipped). Live, (ii) applies to assists only; the rebounds "
           "row is for information. Verdicts (pretip): "
           f"(i) {v_i}; (ii) {v_ii}; (iii) {v_iii}.", "",
           "## The full card three ways (top 3 / top 5 a day)", "", *HEAD]
    for pt in POINTS:
        for n in (3, 5):
            c = C2[pt][f"card_top{n}"]
            md += [_row(f"{pt} top {n}: Ranking v2 (assists + rebounds)", c["ranking_v2"]),
                   _row(f"{pt} top {n}: + Rules v2 (today's OG)", c["ranking_v2_plus_rules_v2"]),
                   _row(f"{pt} top {n}: + Rules v2 + threes", c["plus_threes"])]
    md += ["", "## Pool checks (every priced row, pretip; no true pre-tip close for most rows)",
           "", "| Market | Rows | Brier model | Brier no-vig open | EV at open no-vig, top 5 |",
           "|---|---|---|---|---|"]
    for mk in MARKETS:
        x = M[mk]["pretip"]
        md.append(f"| {mk} | {x['pool_checks']['rows']} | {x['pool_checks']['brier_model']} | "
                  f"{x['pool_checks']['brier_novig']} | "
                  f"{100 * (x['top5']['ev_open'] or 0):+.1f}% |")
    md += ["", "EV at the opening no-vig is roughly minus the vig by construction (the pick's "
           "price is the opening price); it is reported because no pre-tip close exists for "
           "most historical rows (ESPN keeps the opening line and an in-game quote).", "",
           "### Calibration of P(over), pretip (every priced row)", "",
           "| Market | Bin | n | Predicted | Actual |", "|---|---|---|---|---|"]
    for mk in MARKETS:
        for b in M[mk]["pretip"]["pool_checks"]["calibration"]:
            md.append(f"| {mk} | {b['bin']} | {b['n']} | {b['pred']} | {b['actual']} |")
    md += ["", "### Walk-forward F-style Platt fit on the picked side (top 5, pretip)", "",
           "Fitted per season on the earlier seasons' candidates only (2022-23 first). The fit "
           "is increasing, so the ranking is unchanged (checked).", "",
           "| Market | Season | Fit on (n) | Slope | Raw mean p | Calibrated expectation | "
           "Actual win | Ranking unchanged |", "|---|---|---|---|---|---|---|---|"]
    for mk in MARKETS:
        for s, x in M[mk]["pretip"]["platt_walkforward_top5"].items():
            if x.get("slope") is None:
                continue
            md.append(f"| {mk} | {s} | {x['n']} | {x['slope']} | {100 * x['raw_mean_p']:.1f}% | "
                      f"{100 * x['expected_win']:.1f}% | {100 * (x['actual_win'] or 0):.1f}% | "
                      f"{x['ranking_unchanged']} |")
    md += ["", "## Backtest vs live (the discount)", "",
           "Live = the ledger's F picks since Oct 29, one per player-game-market (graded only, "
           "1u). Backtest = this pipeline's F-style candidates (pretip, before Ranking v2).", "",
           "| Market | Live W-L | Live win % | Live ROI | Backtest win % | Backtest ROI | Gap win | "  # noqa: E501
           "Gap ROI |", "|---|---|---|---|---|---|---|---|"]
    for mk in ("rebounds", "assists"):
        lv, g = live[mk], extra["gap"][mk]
        b = M[mk]["pretip"]["candidates"]
        md.append(f"| {mk} | {lv['wl']} | {100 * lv['win']:.1f}% | {100 * lv['roi']:+.1f}% | "
                  f"{100 * b['win']:.1f}% | {100 * b['roi']:+.1f}% | {g['win_pp']:+.1f} pp | "
                  f"{g['roi_pp']:+.1f} pp |")
    wf = extra.get("walkforward")
    if wf:
        md += ["", f"Ranking v2 style: the walk-forward tool's Ranking v2 top 5 on the live ledger "
               f"(Oct 31 - Nov 11, in-sample for the rule design) went {wf}; this pipeline's "
               "today's-OG card (Ranking v2 + Rules v2, top 5, pretip) went "
               f"{wl(C2['pretip']['card_top5']['ranking_v2_plus_rules_v2'])} "
               f"({100 * C2['pretip']['card_top5']['ranking_v2_plus_rules_v2']['win']:.1f}%)."]
    rep = extra["replication"]
    md += ["", "## Checks", "",
           f"- **The rule replicates live OG Ranking v2:** the same top 5 on {rep['same']} of "
           f"{rep['of']} live card days (Oct 31 - Nov 11, the ledger pool); the one difference "
           f"({rep['diff']}) is a full tie — the same bet at DraftKings and FanDuel, same price, "
           "probability and creation second — where the live code keeps one book and the replica "
           "the other.",
           f"- **Reconciles with the stored backtests:** under the M15 rule (10 pp, no Ranking v2) "
           f"this pipeline gives {extra['reconcile']}, matching props_median_backtest.json within "
           "one bet (props_final_test.json predates the M16 line audit; "
           "props_final_test_m16_audit.json differs by under 1% as the stored lines grew).",
           "- **No leakage:** the model is nbap15's final-test fit (2015-16 to 2022-23); the "
           "opening line was posted before both prediction points; the Platt checks fit only "
           "earlier seasons; the teammates rule reads key players from games before the point "
           "and injury reports known at it (tests/test_threes_bt.py).",
           "- F's consensus check (other books not siding against) cannot be replayed: the "
           "history keeps one book per line.", "",
           "## Options for the owner (not decided here)", "",
           "1. **Threes as Action-only picks:** evidence for — threes top 5 "
           f"{100 * t['top5']['all']['win']:.1f}% / {100 * t['top5']['all']['roi']:+.1f}% on the old "  # noqa: E501
           "seasons, lines already flowing, no change to OG's record; against — a rebounds-sized "
           "live discount would erase the edge, and threes picks are nearly all unders.",
           "2. **Threes on the official OG card after an earn-in bar** (e.g. live Action threes "
           "at .500+ and positive units over a fixed number of graded picks): for — it raises the "
           "full card's win rate and picks per day in the backtest; against — the official "
           "record must stay above .500, and rebounds show how far live can fall below a backtest.",
           "3. **Threes not worth it:** for — Brier worse than the no-vig line, thin ROI, 0.5 "
           "lines and bench shooters lose; against — it is the best-performing third market "
           "under today's rules and costs no new data.",
           "",
           f"- **Rule (i) unders 5.5+ assists:** {v_i} on old seasons — keep or revisit at the "
           "Filters review (its removed picks were profitable, but less each season).",
           f"- **Rule (ii) teammates out:** {v_ii} — keep or revisit at the Filters review.",
           f"- **Rule (iii) no rebounds:** {v_iii} on old seasons, but live rebounds lost — the "
           "case for keeping it rests on live data, the case for revisiting on the backtest.",
           "", "Nothing here changes a live rule; the owner decides at the Filters review.", ""]
    return "\n".join(md) + "\n"


def live_f(out: Path) -> dict:
    """The live ledger's F picks since Oct 29, one per player-game-market (highest model
    probability), graded only, 1u: per market W-L, win rate and ROI."""
    from betdata.ledger import counted, joined

    best: dict = {}
    for r in counted(joined(out)):
        if (r["signal_name"] != "F" or r["created_at"] < "2026-10-29"
                or r.get("result") not in ("win", "loss", "push")):  # fmt: skip
            continue
        k = (r["game_id"], r["player"].lower(), r["market"])
        if k not in best or float(r["fair_prob"]) > float(best[k]["fair_prob"]):
            best[k] = r
    res = {}
    for mk in ("rebounds", "assists"):
        rs = [r for r in best.values() if r["market"] == mk]
        w = sum(r["result"] == "win" for r in rs)
        lo = sum(r["result"] == "loss" for r in rs)
        res[mk] = {"picks": len(rs), "wl": f"{w}-{lo}", "win": w / (w + lo) if w + lo else None,
                   "roi": sum(float(r["profit"] or 0) for r in rs) / len(rs) if rs else None}
    return res


def build(conn, out: Path, R: dict, availability: dict, reconcile: str) -> tuple[str, dict]:
    """(markdown, json) of the report from analyse()'s results and the checks."""
    from betdata import walkforward as WF

    live = live_f(out)
    gap = {mk: {"win_pp": round(100 * (live[mk]["win"] - R["markets"][mk]["pretip"]
                                       ["candidates"]["win"]), 1),
                "roi_pp": round(100 * (live[mk]["roi"] - R["markets"][mk]["pretip"]
                                       ["candidates"]["roi"]), 1)} for mk in live}  # fmt: skip
    rep = replicate_live(out)
    diff = ", ".join(d for d, x in rep["days"].items() if not x["same"]) or "none"
    data = WF.load(out)
    wfr = WF.metrics(WF.run(data, "v2top5"))
    wf_txt = (f"{wfr.rec.wlp} ({100 * wfr.rec.wins / max(1, wfr.rec.wins + wfr.rec.losses):.1f}%,"
              f" {100 * (wfr.rec.roi or 0):+.1f}% ROI at 0.5u)")
    extra = {"live": live, "gap": gap, "availability": availability,
             "replication": {"same": rep["same"], "of": rep["of"], "diff": diff},
             "reconcile": reconcile, "walkforward": wf_txt}
    md = report(R, extra)
    js = {"label": "RESEARCH ONLY; hindsight backtest on 2023-24 to 2025-26 at the opening "
                   "line (nothing fitted or chosen on them); live-ledger comparison labelled",
          "written": now_iso(),
          "rule": {"thresholds_pp": THRESHOLD, "price": [PRICE_MIN, PRICE_MAX],
                                          "median": "nbap15 negative binomial median on the side",
                                          "rank": "model probability, ties shorter price"},
          "results": _jsonable(R), "extra": _jsonable(extra),
          "replication_days": _jsonable(rep["days"])}
    return md, js


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return None if np.isnan(x) else float(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return nan_to_none(x)
