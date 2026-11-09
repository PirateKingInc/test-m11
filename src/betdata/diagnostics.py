"""Rules v2 diagnostics (owner decision 2026-11-09, Part 3): REPORTING ONLY. Nothing here
changes a rule, a model or a pick; it explains why losses are concentrated.

Two halves:
- `ledger_part(out)`: from the ledger alone (runs anywhere): role expansion (item 2), home vs
  away unders live (item 4, live half) and Next Gen's calibration and accuracy vs the close by
  market and size of disagreement (item 5).
- `db_part(conn)`: from the full database (run on a runner with the restored state): league
  rebounding (1a), the prop model's projection error by player type this season vs past seasons
  (1b), the M16 audit's rebound backtest by season (1c), primary ballhandlers' assist error
  (item 3), home vs away unders in past seasons (item 4, past half) and rules a-c applied to
  the stored-opening-line seasons (item 6, a check on rules chosen from 2026 data).
`report(ledger, db)` writes reports/nba/rules_v2_diagnostics.md.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

OG_SIGNALS = ("F", "C-NBA")


def _f(x):
    try:
        return None if x in ("", None) else float(x)
    except ValueError:
        return None


def _rec(rows: list[dict]) -> dict:
    w = sum(r["result"] == "win" for r in rows)
    lo = sum(r["result"] == "loss" for r in rows)
    units = sum(_f(r.get("profit")) or 0.0 for r in rows if r["result"] in ("win", "loss",
                                                                            "push"))
    return {"w": w, "l": lo, "n": w + lo, "units": round(units, 2),
            "win": None if not w + lo else round(w / (w + lo), 3)}  # fmt: skip


def _wl(d: dict) -> str:
    return f"{d['w']}-{d['l']}, {d['units']:+.1f}u" if d["n"] else "–"


# --- the ledger half ---


def _teammates_out(statuses: str, player: str) -> int | None:
    try:
        st = json.loads(statuses or "{}")
    except ValueError:
        return None
    if not st:
        return None
    me = (player or "").lower()
    return sum(v in ("Out", "Doubtful") for k, v in st.items() if k.lower() != me)


def ledger_part(out: Path) -> dict:
    from betdata import official as O
    from betdata.ledger import read_rows

    rows = [r for r in O.dataset(out) if r["sport"] == "nba"]
    rp = {r["pick_id"]: r for r in read_rows(out / "review_pick.csv")}
    rr = {r["pick_id"]: r for r in read_rows(out / "review_result.csv")}
    og = [r for r in rows if r["signal_name"] in OG_SIGNALS and r["player"]
          and r.get("result") in ("win", "loss", "push")]  # fmt: skip
    res: dict = {"og_props_graded": len(og), "role": {}, "home_away": {}}
    # 2. role expansion: unders by teammates out and by minutes vs projection
    for mk in ("assists", "rebounds"):
        unders = [r for r in og if r["market"] == mk and r["side"] == "under"]
        by_tm: dict = {}
        by_min: dict = {}
        over5 = {"tm2": [0, 0], "tm01": [0, 0]}
        for r in unders:
            p, q = rp.get(r["pick_id"], {}), rr.get(r["pick_id"], {})
            tm = _teammates_out(p.get("statuses", ""), r["player"])
            k = "unknown" if tm is None else ("2+" if tm >= 2 else str(tm))
            by_tm.setdefault(k, []).append(r)
            pm, am = _f(p.get("proj_minutes")), _f(q.get("actual_minutes"))
            if pm is None or am is None:
                mk_ = "unknown"
            else:
                dm = am - pm
                mk_ = "5+ above" if dm >= 5 else ("5+ below" if dm <= -5 else "within 5")
                if tm is not None:
                    b = over5["tm2" if tm >= 2 else "tm01"]
                    b[0] += dm >= 5
                    b[1] += 1
            by_min.setdefault(mk_, []).append(r)
        res["role"][mk] = {
            "teammates_out": {k: _rec(v) for k, v in sorted(by_tm.items())},
            "minutes_vs_projection": {k: _rec(v) for k, v in sorted(by_min.items())},
            "overshoot_rate": {k: (None if not n else round(a / n, 3), n)
                               for k, (a, n) in over5.items()}}  # fmt: skip
    # 4. home vs away unders (live): the player's team vs the game's home team
    for mk in ("assists", "rebounds", "all"):
        sel = [r for r in og if r["side"] == "under" and (mk == "all" or r["market"] == mk)]
        home, away = [], []
        for r in sel:
            team = rp.get(r["pick_id"], {}).get("team") or r.get("team", "")
            hm = (r["game"] or "? @ ?").split(" @ ")[-1]
            (home if team == hm else away).append(r)
        res["home_away"][mk] = {"home": _rec(home), "away": _rec(away)}
    res["engine"] = engine_part(read_rows(out / "engine_grades.csv"))
    return res


def engine_part(grades: list[dict]) -> dict:
    """5. Next Gen's calibration and accuracy vs the close by market and by disagreement."""
    from betdata.nba.engine_grades import band_table, summary

    out = {"overall": None, "by_market": {}, "by_gap": {}, "bands": band_table(grades)}
    s = summary(grades)
    out["overall"] = {"n": s.n, "brier_engine": s.brier_engine, "brier_close": s.brier_close,
                      "diff": s.diff, "ece_engine": s.ece_engine, "ece_close": s.ece_close}
    for mk in sorted({r["market"] for r in grades}):
        s = summary([r for r in grades if r["market"] == mk])
        out["by_market"][mk] = {"n": s.n, "brier_engine": s.brier_engine,
                                "brier_close": s.brier_close, "diff": s.diff,
                                "verdict": s.verdict}  # fmt: skip
    # by the engine's disagreement with the close, on the side the engine favours vs the close
    gaps: dict = {"0-4 pts": [], "4-8 pts": [], "8+ pts": []}
    for r in grades:
        if r.get("y") not in ("0", "1"):
            continue
        pe, pc, y = float(r["p_engine"]), float(r["p_close"]), int(r["y"])
        gap = pe - pc
        side_p, side_c, side_y = (pe, pc, y) if gap >= 0 else (1 - pe, 1 - pc, 1 - y)
        k = "8+ pts" if abs(gap) >= 0.08 else ("4-8 pts" if abs(gap) >= 0.04 else "0-4 pts")
        gaps[k].append((side_p, side_c, side_y))
    for k, v in gaps.items():
        n = len(v)
        out["by_gap"][k] = {"n": n} if not n else {
            "n": n, "hit": round(sum(y for *_, y in v) / n, 3),
            "engine": round(sum(p for p, *_ in v) / n, 3),
            "close": round(sum(c for _, c, _ in v) / n, 3)}
    return out


# --- the database half (a runner) ---


def _season_of(t) -> int:
    return t.year if t.month >= 8 else t.year - 1


def db_part(conn, out: Path | None = None) -> dict:
    """Items 1a, 1b, 1c, 3, 4 (past) and 6. Heavy: run where the full state is restored."""
    import numpy as np
    import pandas as pd
    from sqlalchemy import select

    from betdata.db.schema import games, nba_player_games, prop_grades, prop_predictions

    res: dict = {}
    box = pd.read_sql(select(nba_player_games.c.game_id, nba_player_games.c.team_id,
                             nba_player_games.c.player_id, nba_player_games.c.player_name,
                             nba_player_games.c.starter, nba_player_games.c.minutes,
                             nba_player_games.c.fga, nba_player_games.c.fgm,
                             nba_player_games.c.fta, nba_player_games.c.ftm,
                             nba_player_games.c.oreb, nba_player_games.c.dreb,
                             nba_player_games.c.ast, games.c.start_time_utc,
                             games.c.home_team_id)
                      .join(games, games.c.game_id == nba_player_games.c.game_id), conn)
    box["tip"] = pd.to_datetime(box.start_time_utc, utc=True)
    box["season"] = box.tip.dt.year.where(box.tip.dt.month >= 8, box.tip.dt.year - 1)
    box["reb"] = box.oreb.fillna(0) + box.dreb.fillna(0)
    # 1a. league rebounding by season: per team-game, and per missed shot (FG misses + an
    # approximate 0.44 x FT misses), and the offensive share
    tg = box.groupby(["season", "game_id", "team_id"]).agg(
        reb=("reb", "sum"), oreb=("oreb", "sum"), fga=("fga", "sum"), fgm=("fgm", "sum"),
        fta=("fta", "sum"), ftm=("ftm", "sum")).reset_index()  # fmt: skip
    tg["missed"] = (tg.fga - tg.fgm) + 0.44 * (tg.fta - tg.ftm)
    res["1a_league_rebounding"] = {
        int(s): {"team_games": int(len(g)), "reb_per_team_game": round(g.reb.mean(), 2),
                 "reb_per_missed_shot": round(g.reb.sum() / g.missed.sum(), 4),
                 "oreb_share": round(g.oreb.sum() / g.reb.sum(), 4),
                 "fga_per_team_game": round(g.fga.mean(), 2)}
        for s, g in tg.groupby("season") if len(g) > 20}  # fmt: skip
    # player types per season: big / guard / wing by per-36 rates; starter / bench by majority
    pl = box[box.minutes.fillna(0) > 0].groupby(["season", "player_id"]).agg(
        mins=("minutes", "sum"), reb=("reb", "sum"), ast=("ast", "sum"),
        starts=("starter", "mean")).reset_index()  # fmt: skip
    pl["r36"], pl["a36"] = 36 * pl.reb / pl.mins, 36 * pl.ast / pl.mins
    pl["type"] = np.where(pl.r36 >= 9, "big", np.where(pl.a36 >= 5, "guard", "wing"))
    pl["role"] = np.where(pl.starts >= 0.5, "starter", "bench")
    kinds = (pl.set_index(["season", "player_id"])[["type", "role"]]
             .rename(columns={"type": "ptype", "role": "prole"}))
    # 1b. this season's projection error (nbap15 live predictions vs actuals)
    pp = pd.read_sql(select(prop_predictions.c.prop_prediction_id, prop_predictions.c.game_id,
                            prop_predictions.c.player_id, prop_predictions.c.market,
                            prop_predictions.c.mean, prop_predictions.c.dist,
                            prop_predictions.c.prediction_point, prop_grades.c.actual)
                     .join(prop_grades, prop_grades.c.prop_prediction_id
                           == prop_predictions.c.prop_prediction_id)
                     .where(prop_predictions.c.model == "nbap15",
                            prop_grades.c.actual.is_not(None)), conn)  # fmt: skip
    res["1b_live_projection_error"] = {}
    if len(pp):
        pp = pp.merge(box[["game_id", "player_id", "minutes", "season", "home_team_id",
                           "team_id"]].drop_duplicates(["game_id", "player_id"]),
                      on=["game_id", "player_id"], how="left")  # fmt: skip

        def proj_minutes(d):
            try:
                x = d if isinstance(d, dict) else json.loads(d or "{}")
            except (TypeError, ValueError):
                return None
            return x.get("minutes") or x.get("m_hat")

        pp["proj_min"] = pp.dist.map(proj_minutes)
        pp = pp.join(kinds, on=["season", "player_id"])
        for mk in ("rebounds", "assists", "points"):
            d = pp[pp.market == mk]
            if d.empty:
                continue
            d = d.assign(err=d.actual - d["mean"],
                         merr=d.minutes - pd.to_numeric(d.proj_min, errors="coerce"))
            res["1b_live_projection_error"][mk] = {
                f"{t}/{r}": {"n": int(len(g)), "mean_err": round(g.err.mean(), 2),
                             "mae": round(g.err.abs().mean(), 2),
                             "minutes_err": None if g.merr.isna().all()
                             else round(g.merr.mean(), 2)}
                for (t, r), g in d.groupby(["ptype", "prole"])}  # fmt: skip
    # 1b (past): the props backtest's error by player type, season by season
    try:
        res.update(_backtest_parts(conn, kinds, box))
    except Exception as e:  # noqa: BLE001 - a missing history only drops that part
        res["backtest_error"] = f"{type(e).__name__}: {e}"
    # 1c. the M16 audit's rebound backtest by season, as reported then
    try:
        a = json.loads((Path("reports/nba") / "props_final_test_m16_audit.json").read_text())
        reb = (a["markets"].get("rebounds") or {}).get("pretip_at_open") or {}
        res["1c_m16_audit_rebounds"] = {"bets": reb.get("bets"),
                                        "by_season": reb.get("by_season")}  # fmt: skip
    except Exception as e:  # noqa: BLE001
        res["1c_m16_audit_rebounds"] = f"unavailable: {e}"
    return res


def _backtest_parts(conn, kinds, box) -> dict:
    """1b past, 3, 4 past and 6 from the props backtest (opening lines 2022-23 on)."""
    import pandas as pd

    from betdata.nba.props import model as M
    from betdata.nba.props import select as S

    out: dict = {}
    f = S.usable(S.history_features())
    tune = json.loads((S.REPORTS / "props_tune.json").read_text())
    mod = M.fit(f[f.season.isin((*S.TRAIN, *S.VALIDATE))])
    for mk, r in tune["markets"].items():
        if r.get("calibration_applied"):
            mod.calib[mk] = tuple(r["platt"])
    lines = S.history_lines(conn)
    seasons = sorted(int(s) for s in f.season.unique() if s >= 2022)
    te = f[f.season.isin(seasons) & (f.point == "pretip")]
    rows = []
    for mk in ("rebounds", "assists"):
        d = S.priced(mod, te, lines, mk, S.LINE)
        if d.empty:
            continue
        th = (tune["markets"].get(mk) or {}).get("threshold")
        th = th if th is not None else 4.0
        b = S.bets(d, th)
        b = b.assign(market=mk, side=lambda x: x.edge.map(lambda e: "over" if e > 0 else
                                                           "under"))  # fmt: skip
        rows.append(b)
        # 1b (past): error by player type and season, every priced row
        d = d.join(kinds, on=["season", "player_id"])
        out.setdefault("1b_past_projection_error", {})[mk] = {
            f"{int(s)} {t}/{r}": {"n": int(len(g)), "mean_err": round((g.actual - g.mu).mean(), 2),
                                  "mae": round((g.actual - g.mu).abs().mean(), 2)}
            for (s, t, r), g in d.groupby(["season", "ptype", "prole"]) if len(g) >= 50}
    if not rows:
        return out
    bt = pd.concat(rows, ignore_index=True)
    # home / away from the box
    home = box.drop_duplicates(["game_id", "player_id"]).set_index(["game_id", "player_id"])
    cols = ["home_team_id"] + ([] if "team_id" in bt.columns else ["team_id"])
    bt = bt.join(home[cols], on=["game_id", "player_id"])
    bt["home"] = bt.team_id == bt.home_team_id

    def rec(g):
        w, lo = int((g.result == "win").sum()), int((g.result == "loss").sum())
        return {"w": w, "l": lo, "units": round(float(g.profit.sum()), 1),
                "roi": None if not len(g) else round(float(g.profit.sum()) / len(g), 3)}

    out["4_past_home_away_unders"] = {
        f"{int(s)} {'home' if h else 'away'}": rec(g)
        for (s, h), g in bt[bt.side == "under"].groupby(["season", "home"])}
    # 6. rules a-c applied to past seasons (a check on rules chosen from 2026 data); rule (c)
    # uses the team's key players (>= 24 min over their last 10) who did not play that game -
    # an ex-post stand-in for "Out/Doubtful at pick time", since past injury reports may not
    # cover these seasons
    key_out = _key_out_ex_post(box)
    bt["tm_out"] = [key_out.get((g, t, p), 0) for g, t, p in
                    zip(bt.game_id, bt.team_id, bt.player_id, strict=True)]  # fmt: skip

    def rule(r):
        if r.market == "rebounds":
            return "filtered: rebounds"
        if r.side == "under" and r.line >= 5.5:
            return "filtered: under 5.5+ assists"
        if r.side == "under" and r.tm_out >= 2:
            return "filtered: teammates out (ex post)"
        return "kept"

    bt["rule"] = bt.apply(rule, axis=1)
    out["6_rules_on_past_seasons"] = {
        f"{int(s)} {k}": rec(g) for (s, k), g in bt.groupby(["season", "rule"])}
    out["6_rules_on_past_seasons_all"] = {k: rec(g) for k, g in bt.groupby("rule")}
    out["6_before_rules"] = {int(s): rec(g) for s, g in bt.groupby("season")}
    # 3. primary ballhandlers: assist error for each team's leading assist player per season
    lead = (box.groupby(["season", "team_id", "player_id"]).ast.mean().reset_index()
            .sort_values("ast").drop_duplicates(["season", "team_id"], keep="last"))
    leaders = set(zip(lead.season, lead.player_id, strict=True))
    d = S.priced(mod, te, lines, "assists", S.LINE)
    if not d.empty:
        d["leader"] = [(s, p) in leaders for s, p in zip(d.season, d.player_id, strict=True)]
        out["3_primary_ballhandlers_assists"] = {
            f"{int(s)} {'leader' if ld else 'others'}": {
                "n": int(len(g)), "mean_err": round((g.actual - g.mu).mean(), 2),
                "mae": round((g.actual - g.mu).abs().mean(), 2),
                "over_rate": round(float(g.over.mean()), 3)}
            for (s, ld), g in d.groupby(["season", "leader"])}
    return out


def _key_out_ex_post(box) -> dict:
    """(game, team, player) -> key teammates (>= 24 min over their last 10 games) who did not
    play that game."""
    import pandas as pd

    b = box.sort_values("tip")
    b["avg10"] = (b.groupby("player_id").minutes
                  .transform(lambda s: s.shift(1).rolling(10, min_periods=3).mean()))
    keys = b[b.avg10 >= 24]
    played = set(zip(b.game_id[b.minutes.fillna(0) > 0], b.player_id[b.minutes.fillna(0) > 0],
                     strict=True))  # fmt: skip
    # a key player is "out" for a game of his team if he has no minutes in it
    team_games = b[["game_id", "team_id", "tip"]].drop_duplicates()
    last_key = keys.groupby(["team_id", "player_id"]).tip.max().reset_index()
    out: dict = {}
    first_key = keys.groupby(["team_id", "player_id"]).tip.min().reset_index()
    span = first_key.merge(last_key, on=["team_id", "player_id"], suffixes=("_a", "_b"))
    for tm, g in team_games.groupby("team_id"):
        ks = span[span.team_id == tm]
        for gid, tip in zip(g.game_id, g.tip, strict=True):
            n = sum(1 for p, a, z in zip(ks.player_id, ks.tip_a, ks.tip_b, strict=True)
                    if a <= tip <= z + pd.Timedelta(days=30) and (gid, p) not in played)
            if n:
                for p in b.player_id[(b.game_id == gid) & (b.team_id == tm)]:
                    out[(gid, tm, p)] = n
    return out


# --- the report ---


def summary(ledger: dict, db: dict | None) -> list[str]:
    """The headline findings, from the numbers below (reporting only)."""
    out = []
    ra = ledger["role"]
    for mk in ("assists", "rebounds"):
        t = ra[mk]["teammates_out"].get("2+")
        m = ra[mk]["minutes_vs_projection"].get("5+ above")
        if t and m:
            out.append(f"Live {mk} unders: with 2+ key teammates out {_wl(t)}; when the player "
                       f"played 5+ minutes above projection {_wl(m)}.")
    ha = ledger["home_away"]["all"]
    out.append(f"Live unders home {_wl(ha['home'])}, away {_wl(ha['away'])}.")
    g8 = ledger["engine"]["by_gap"].get("8+ pts") or {}
    if g8.get("n"):
        out.append(f"Next Gen 8+ points from the close: its side hit {_pct(g8['hit'])} "
                   f"(engine {_pct(g8['engine'])}, close {_pct(g8['close'])}).")
    if not db:
        return out
    la = db.get("1a_league_rebounding") or {}
    if la:
        cur = la[max(la, key=int)]
        past = [v for k, v in la.items() if k != max(la, key=int)]
        out.append(f"League rebounding this season: {cur['reb_per_team_game']} per team-game "
                   f"(past {min(v['reb_per_team_game'] for v in past)}-"
                   f"{max(v['reb_per_team_game'] for v in past)}); per missed shot "
                   f"{cur['reb_per_missed_shot']} (past "
                   f"{min(v['reb_per_missed_shot'] for v in past)}-"
                   f"{max(v['reb_per_missed_shot'] for v in past)}); offensive share "
                   f"{cur['oreb_share']} (past {min(v['oreb_share'] for v in past)}-"
                   f"{max(v['oreb_share'] for v in past)}).")
    allr = db.get("6_rules_on_past_seasons_all") or {}
    if allr:
        out.append("Rules a-c replayed on 2022-23 to 2025-26 opening lines (a check on rules "
                   "chosen from 2026 data; 2022-23 is in-sample for nbap15): " + "; ".join(
                       f"{k}: {v['w']}-{v['l']}, {v['units']:+}u (ROI {v['roi']})"
                       for k, v in sorted(allr.items())) + ". In those seasons rules (a) and "
                   "(b) would have removed profitable bets; only (c) removed losing ones.")
    return out


def report(ledger: dict, db: dict | None) -> str:
    md = ["# Rules v2 diagnostics (reporting only)", "",
          "Owner decision 2026-11-09, Part 3. Nothing here changes a rule or a model. Rules v2 "
          "were chosen from the 2026 live ledger; every check on past seasons is labelled as "
          "a check on rules chosen from 2026 data.", "", "## Summary", ""]
    md += [f"- {x}" for x in summary(ledger, db)]
    if db and db.get("sources"):
        md += ["", "Sources: " + "; ".join(f"{k}: {v}" for k, v in db["sources"].items())]
    md.append("")
    md += ["## 2. Role expansion (live OG prop unders)", ""]
    for mk, d in ledger["role"].items():
        md.append(f"**{mk}** — by key teammates Out/Doubtful at pick time: "
                  + "; ".join(f"{k}: {_wl(v)}" for k, v in d["teammates_out"].items()))
        md.append("by minutes vs projection: "
                  + "; ".join(f"{k}: {_wl(v)}" for k, v in d["minutes_vs_projection"].items()))
        o = d["overshoot_rate"]
        md.append(f"minutes 5+ above projection: with 2+ teammates out "
                  f"{_pct(o['tm2'][0])} of {o['tm2'][1]}; with 0-1 out {_pct(o['tm01'][0])} of "
                  f"{o['tm01'][1]}")
        md.append("")
    md += ["## 4. Home vs away unders (live OG props)", ""]
    for mk, d in ledger["home_away"].items():
        md.append(f"- {mk}: home {_wl(d['home'])}; away {_wl(d['away'])}")
    md += ["", "## 5. Next Gen: calibration and accuracy vs the close", ""]
    e = ledger["engine"]
    o = e["overall"]
    md.append(f"- Overall: {o['n']} graded; Brier engine {_n(o['brier_engine'])} vs close "
              f"{_n(o['brier_close'])}")
    for mk, d in e["by_market"].items():
        md.append(f"- {mk}: {d['n']} graded; Brier engine {_n(d['brier_engine'])} vs close "
                  f"{_n(d['brier_close'])} — {d['verdict']}")
    for k, d in e["by_gap"].items():
        if d["n"]:
            md.append(f"- Engine vs close gap {k}: {d['n']} predictions; the engine's side hit "
                      f"{_pct(d['hit'])} (engine said {_pct(d['engine'])}, close "
                      f"{_pct(d['close'])})")
    md.append("- Bands: " + "; ".join(
        f"{b['band']}: {_pct(b['hit'])} hit vs {_pct(b['predicted'])} ({b['n']})"
        for b in e["bands"]))
    if db is None:
        md += ["", "Items 1, 3, 4 (past seasons) and 6 need the full database: run "
               "`betdata diag rules-v2 --db` on a runner."]
        return "\n".join(md) + "\n"
    md += ["", "## 1a. League rebounding by season", "",
           "| Season | Team-games | Reb / team-game | Reb / missed shot | OREB share | FGA |",
           "|---|---|---|---|---|---|"]
    for s, d in sorted(db.get("1a_league_rebounding", {}).items()):
        md.append(f"| {s} | {d['team_games']} | {d['reb_per_team_game']} | "
                  f"{d['reb_per_missed_shot']} | {d['oreb_share']} | {d['fga_per_team_game']} |")
    md += ["", "## 1b. Projection error (actual minus projected) by player type", ""]
    for mk, d in db.get("1b_live_projection_error", {}).items():
        md.append(f"- live {mk}: " + "; ".join(
            f"{k} n={v['n']} err {v['mean_err']:+} (MAE {v['mae']}, min err "
            f"{v['minutes_err'] if v['minutes_err'] is not None else '–'})"
            for k, v in sorted(d.items())))
    for mk, d in db.get("1b_past_projection_error", {}).items():
        md.append(f"- past {mk}: " + "; ".join(
            f"{k} n={v['n']} err {v['mean_err']:+}" for k, v in sorted(d.items())))
    md += ["", "## 1c. The M16 audit's rebound backtest (opening lines)", "",
           f"{json.dumps(db.get('1c_m16_audit_rebounds'))}", ""]
    md += ["## 3. Primary ballhandlers: assist projection error", ""]
    for k, v in sorted(db.get("3_primary_ballhandlers_assists", {}).items()):
        md.append(f"- {k}: n={v['n']} err {v['mean_err']:+} (MAE {v['mae']}), over rate "
                  f"{_pct(v['over_rate'])}")
    md += ["", "## 4 (past). Home vs away unders in the props backtest", ""]
    for k, v in sorted(db.get("4_past_home_away_unders", {}).items()):
        md.append(f"- {k}: {v['w']}-{v['l']}, {v['units']:+}u (ROI {v['roi']})")
    md += ["", "## 6. Rules a-c on past seasons (a check on rules chosen from 2026 data)", "",
           "Rule (c) uses an ex-post stand-in (key teammates who did not play).", ""]
    for k, v in sorted(db.get("6_before_rules", {}).items()):
        md.append(f"- {k} all backtested rebound+assist bets: {v['w']}-{v['l']}, "
                  f"{v['units']:+}u (ROI {v['roi']})")
    for k, v in sorted(db.get("6_rules_on_past_seasons", {}).items()):
        md.append(f"- {k}: {v['w']}-{v['l']}, {v['units']:+}u (ROI {v['roi']})")
    if db.get("backtest_error"):
        md.append(f"- backtest parts unavailable: {db['backtest_error']}")
    return "\n".join(md) + "\n"


def _pct(x) -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.1f}%"


def _n(x) -> str:
    return "–" if x is None else f"{x:.4f}"


def _median(xs):
    return statistics.median(xs) if xs else None
