"""M17 Part F: fantasy benchmarks vs the engine, and the ff_opportunity input test
(MODEL.md "M17 F results"). Analysis only; nothing here runs live.

  python -m betdata.nfl.engine.fantasy <fantasy dir> <ep_weekly dir>

<fantasy dir>: the files the fetch workflow put on the m17-data branch (sleeper_proj_<season>.json,
sleeper_players.json, ff_rankings_db_fpecr.parquet); <ep_weekly dir>: nflverse ff_opportunity
ep_weekly_<season>.parquet. Engine: prekick predictions of the validated design on 2020-2022
(data/derived/m17/val_adj1) and of the final test on 2023-2025 (data/derived/m17/final_test).
Writes reports/nfl/m17_fantasy.json.
"""

import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from betdata.nfl.engine.plays import load

F = Path(sys.argv[1])  # fantasy files dir
FFO = Path(sys.argv[2])  # ep_weekly_*.parquet dir
OUT = Path("reports/nfl/m17_fantasy.json")
STATS = {"targets": ("targets", "rec_tgt"), "receptions": ("receptions", "rec"),
         "rec_yards": ("rec_yards", "rec_yd"), "carries": ("carries", "rush_att"),
         "rush_yards": ("rush_yards", "rush_yd")}
VAL, TEST = (2020, 2021, 2022), (2023, 2024, 2025)


def engine():
    parts = []
    for s in VAL:
        parts.append(pd.read_pickle(f"data/derived/m17/val_adj1/players_{s}.pkl"))
    parts.append(pd.read_pickle("data/derived/m17/final_test/players.pkl"))
    e = pd.concat(parts, ignore_index=True)
    return e[e.point == "prekick"]


def actuals(seasons):
    pg = load(seasons)["player_games"]
    return pg[pg.off_snaps.fillna(0) > 0]


def ppr(d, pre):
    g = lambda c: d.get(pre + c, 0).fillna(0)  # noqa: E731
    return (g("receptions") + 0.1 * (g("rec_yards") + g("rush_yards"))
            + 6 * (g("rec_td") + g("rush_td")))


def mae(a, b):
    return round(float(np.abs(a - b).mean()), 3)


def sleeper(eng, act):
    pl = json.load(open(F / "sleeper_players.json"))
    gs = {k: v["gsis_id"].strip() for k, v in pl.items() if v.get("gsis_id")}
    rows = []
    for s in VAL + TEST:
        d = pd.DataFrame(json.load(open(F / f"sleeper_proj_{s}.json")))
        d["player"] = d.sleeper_id.map(gs)
        d["season"] = s
        lm = pd.to_datetime(d.last_modified, unit="ms", utc=True)
        d["modified"] = lm
        rows.append(d.dropna(subset=["player"]))
    sl = pd.concat(rows)
    m = act.merge(eng, on=["game_id", "player", "season"], suffixes=("", "_e"))
    m = m.merge(sl, on=["season", "week", "player"])
    out = {"note": "Sleeper (rotowire) projections, matched by gsis_id on player-weeks the "
                   "player took a snap; last_modified is missing (2020) or after the game's "
                   "kickoff for many rows (share below), so a projection's as-of time is "
                   "unknown: benchmark only, never an input",
           "share_modified_after_kickoff": round(float(
               (m.modified > pd.to_datetime(m.kickoff, utc=True)).mean()), 3)}
    for label, ss in (("2020-2022", VAL), ("2023-2025", TEST)):
        x = m[m.season.isin(ss)]
        res = {"player_weeks": int(len(x))}
        for k, (ac, sc) in STATS.items():
            y = x.dropna(subset=[sc, f"{k}_mean"])
            y = y[y[sc] > 0]
            res[k] = {"n": int(len(y)), "engine_mean": mae(y[ac], y[f"{k}_mean"]),
                      "engine_median": mae(y[ac], y[f"{k}_median"]),
                      "sleeper": mae(y[ac], y[sc])}
        out[label] = res
    return out


def ffo_lag(seasons):
    """Per player-week: the player's season-to-date mean of expected targets, receptions,
    receiving and rushing yards from ff_opportunity, through the previous week only."""
    d = pd.concat([pd.read_parquet(FFO / f"ep_weekly_{s}.parquet") for s in seasons])
    d["season"], d["week"] = d.season.astype(int), d.week.astype(int)
    d = d.rename(columns={"player_id": "player"}).sort_values(["player", "season", "week"])
    cols = {"rec_attempt": "x_targets", "receptions_exp": "x_receptions",
            "rec_yards_gained_exp": "x_rec_yards", "rush_attempt": "x_carries",
            "rush_yards_gained_exp": "x_rush_yards"}
    g = d.groupby(["player", "season"])
    for c, n in cols.items():
        d[n] = g[c].transform(lambda s: s.shift(1).expanding().mean())
    return d[["player", "season", "week", "game_id", *cols.values()]]


def ffo(eng, act):
    lag = ffo_lag(VAL + TEST)
    m = act.merge(eng, on=["game_id", "player", "season"], suffixes=("", "_e"))
    m = m.merge(lag.drop(columns="game_id"), on=["player", "season", "week"])
    out = {"note": "ff_opportunity expected stats are computed after each game from its "
                   "play-by-play; usable pre-game only as the season-to-date mean through the "
                   "previous week (x_*). Benchmark: that mean as a projection. Input test: "
                   "does adding it to the engine's mean lower validation (2020-2022) MAE, "
                   "leave-one-season-out within validation?"}
    bench, test = {}, {}
    for label, ss in (("2020-2022", VAL), ("2023-2025", TEST)):
        x = m[m.season.isin(ss)]
        res = {}
        for k, (ac, _) in STATS.items():
            y = x.dropna(subset=[f"x_{k}", f"{k}_mean"])
            res[k] = {"n": int(len(y)), "engine_mean": mae(y[ac], y[f"{k}_mean"]),
                      "ffo_lag": mae(y[ac], y[f"x_{k}"])}
        bench[label] = res
    x = m[m.season.isin(VAL)]
    for k, (ac, _) in STATS.items():
        y = x.dropna(subset=[f"x_{k}", f"{k}_mean"])
        base, both = [], []
        for hold in VAL:
            tr, te = y[y.season != hold], y[y.season == hold]
            for cols, acc in (([f"{k}_mean"], base), ([f"{k}_mean", f"x_{k}"], both)):
                X = np.column_stack([np.ones(len(tr))] + [tr[c] for c in cols])
                b, *_ = np.linalg.lstsq(X, tr[ac].to_numpy(), rcond=None)
                Xt = np.column_stack([np.ones(len(te))] + [te[c] for c in cols])
                acc.append(np.abs(te[ac].to_numpy() - Xt @ b))
        base, both = np.concatenate(base), np.concatenate(both)
        d = both - base
        test[k] = {"n": int(len(y)), "engine_recalibrated": round(float(base.mean()), 3),
                   "engine_plus_ffo": round(float(both.mean()), 3),
                   "diff": round(float(d.mean()), 3),
                   "diff_se": round(float(d.std(ddof=1) / np.sqrt(len(d))), 3)}
    out["benchmark"] = bench
    out["input_test_validation"] = test
    return out


def norm(s):
    s = re.sub(r"[^a-z ]", "", str(s).lower().replace("-", " "))
    return re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s).split()


def key(name, team):
    n = norm(name)
    return f"{' '.join(n)}|{team}"


def ecr(eng, act):
    r = pd.read_parquet(F / "ff_rankings_db_fpecr.parquet",
                        columns=["page_type", "player", "pos", "team", "ecr", "scrape_date"])
    r = r[r.page_type.isin(["weekly-wr", "weekly-rb", "weekly-te"])].copy()
    r["scrape_date"] = pd.to_datetime(r.scrape_date, utc=True)
    pos = pd.read_parquet("data/nflverse/players/players.parquet",
                          columns=["gsis_id", "display_name", "position"])
    pos = pos.drop_duplicates("gsis_id").set_index("gsis_id")
    m = act.merge(eng, on=["game_id", "player", "season"], suffixes=("", "_e"))
    m["kick"] = pd.to_datetime(m.kickoff, utc=True)
    m["name"] = m.player.map(pos.display_name)
    m["pos"] = m.player.map(pos.position)
    m = m[m.pos.isin(["WR", "RB", "TE"])]
    m["k"] = [key(n, t) for n, t in zip(m.name, m.team, strict=True)]
    r["k"] = [key(n, t) for n, t in zip(r.player, r.team, strict=True)]
    # each week: the last ECR snapshot before that week's first kickoff (as-of)
    wk = m.groupby(["season", "week"]).kick.min().rename("wk_kick").reset_index()
    snaps = np.sort(r.scrape_date.unique())
    wk["snap"] = [snaps[snaps < k].max() if (snaps < k).any() else pd.NaT for k in wk.wk_kick]
    wk = wk[(wk.wk_kick - pd.to_datetime(wk.snap, utc=True)) < pd.Timedelta(days=7)]
    m = m.merge(wk[["season", "week", "snap"]], on=["season", "week"])
    m = m.merge(r[["k", "pos", "ecr", "scrape_date"]].rename(
        columns={"scrape_date": "snap", "pos": "fp_pos"}), on=["k", "snap"])
    m["fp_pos"] = m.fp_pos.str.extract(r"([A-Z]+)")[0]
    m = m[m.fp_pos == m.pos]
    m["actual_ppr"] = ppr(m, "")
    m["engine_ppr"] = (m.receptions_mean.fillna(0) + 0.1 * (m.rec_yards_mean.fillna(0)
                       + m.rush_yards_mean.fillna(0)) + 6 * (m.rec_td_mean.fillna(0)
                       + m.rush_td_mean.fillna(0)))
    out = {"note": "FantasyPros weekly PPR WR/RB/TE expert consensus ranks (nflverse "
                   "ff_rankings), snapshot taken < 7 days before the week's first kickoff; "
                   "matched by name+team. Mean Spearman rank correlation with actual PPR "
                   "points within each position-week, engine expected PPR points vs ECR "
                   "(lower ECR = better). Benchmark only."}
    for label, ss in (("2020-2022", VAL), ("2023-2025", TEST)):
        x = m[m.season.isin(ss)]
        rs_e, rs_f, n = [], [], 0
        for _, g in x.groupby(["season", "week", "pos"]):
            g = g.nsmallest(min(len(g), 40), "ecr")  # the ranked, fantasy-relevant players
            if len(g) < 10:
                continue
            rs_e.append(spearmanr(g.engine_ppr, g.actual_ppr)[0])
            rs_f.append(spearmanr(-g.ecr, g.actual_ppr)[0])
            n += len(g)
        out[label] = {"position_weeks": len(rs_e), "player_weeks": n,
                      "engine_rho": round(float(np.nanmean(rs_e)), 3),
                      "ecr_rho": round(float(np.nanmean(rs_f)), 3),
                      "engine_better_share": round(float(np.mean(
                          np.array(rs_e) > np.array(rs_f))), 3)}
    return out


def main():
    eng = engine()
    act = actuals(VAL + TEST)
    res = {"sleeper": sleeper(eng, act), "ff_opportunity": ffo(eng, act), "ff_rankings":
           ecr(eng, act)}
    OUT.write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
