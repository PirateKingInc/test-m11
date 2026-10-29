"""Agreement rule: (1) NBA backtest of signal (a) on stored history (nba15, out of sample);
(2) the live sample: a copy of the hourly state regraded with the rule (never saved)."""

import json
import traceback
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import select

from betdata.analysis.agreement_backtest import picks, summarize
from betdata.config import get_settings
from betdata.db.engine import make_engine, upgrade

pd.set_option("display.width", 250)
pd.set_option("display.max_rows", 400)

out_json: dict = {}
# --- (2) live sample ----------------------------------------------------------------------------
from betdata.db.schema import (  # noqa: E402
    games,
    model_predictions,
    nba_bet_grades,
    nfl_bet_grades,
    teams,
)
from betdata.sports import active_sports  # noqa: E402

url = get_settings().db_url
upgrade(url)  # 0019/0020 on this copy only
eng = make_engine(url)
now = datetime.now(UTC)
for sport in active_sports():
    with eng.begin() as c:
        try:
            print(f"\nregrade {sport.key}:", sport.grade(c, now))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            print(f"{sport.key} regrade failed:", e)


def rec(y):
    return (f"{(y.result == 'win').sum()}-{(y.result == 'loss').sum()}-"
            f"{(y.result == 'push').sum()} units {y.profit.sum():+.2f}")


for tbl, name in ((nfl_bet_grades, "nfl"), (nba_bet_grades, "nba")):
    mp, h, a = model_predictions, teams.alias("h"), teams.alias("a")
    with eng.connect() as c:
        q = (select(tbl.c.model_version, tbl.c.game_id, tbl.c.side, tbl.c.line, tbl.c.result,
                    tbl.c.profit, tbl.c.agree, mp.c.predicted_at, games.c.start_time_utc,
                    h.c.abbrev.label("home"), a.c.abbrev.label("away"))
             .join(mp, mp.c.prediction_id == tbl.c.prediction_id)
             .join(games, games.c.game_id == tbl.c.game_id)
             .join(h, h.c.team_id == games.c.home_team_id)
             .join(a, a.c.team_id == games.c.away_team_id)
             .where(tbl.c.market == "spread"))  # fmt: skip
        g = pd.DataFrame(c.execute(q).mappings().all())
    if g.empty:
        print(f"\n== live {name}: no spread grades")
        continue
    g = g.sort_values("predicted_at").groupby(["model_version", "game_id"]).tail(1)
    g = g[g.result != "void"]
    print(f"\n== live {name} spread (page rule: last graded pick per game)")
    live = {}
    for v, x in g.groupby("model_version"):
        ag = x[x.agree.fillna("") != ""]
        live[v] = {"all": rec(x), "agreement": rec(ag), "agreement_n": int(len(ag)),
                   "signals": ag.agree.value_counts().to_dict()}
        print(f"  {v:28s} all {rec(x)} | agreement ({len(ag)}) {rec(ag)} | "
              f"signals {ag.agree.value_counts().to_dict()}")
    out_json[f"live {name}"] = live
    print(g[["model_version", "start_time_utc", "away", "home", "side", "line", "agree",
             "result"]].to_string(index=False))
# Historical NBA lines live on the nba-history branch (not in the runner state): load them into
# this copy only after the live regrade above, so they cannot change the live sample.
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

from betdata.nba.history import load as load_history  # noqa: E402

hist = Path("out/_nbahist")
hist.mkdir(parents=True, exist_ok=True)
subprocess.run(["git", "fetch", "-q", "--depth=1", "origin", "nba-history"], check=True)
names = subprocess.run(["git", "ls-tree", "--name-only", "FETCH_HEAD"], check=True,
                       capture_output=True, text=True).stdout.split()
for n in names:
    if n.startswith("odds_"):
        (hist / n).write_bytes(subprocess.run(["git", "show", f"FETCH_HEAD:{n}"], check=True,
                                              capture_output=True).stdout)
print("\nloaded NBA history:", load_history(eng, hist))
# --- (1) NBA backtest ---------------------------------------------------------------------------
try:
    from betdata.nba.pipeline import TEST, TRAIN, VALIDATE, columns
    from betdata.nba.select15 import rows15
    from betdata.nba.selection import fits_for, market_frame, predictions

    d = json.load(open("config/nba15_model.json"))
    store, out, rows, _ = rows15()
    m, t = columns(rows, d["groups"])
    parts = []
    for fit_s, test_s in ((TRAIN, VALIDATE), (TRAIN + VALIDATE, TEST)):
        fits = fits_for(rows, out, m, t, fit_s, d["lam_m"], d["lam_t"])
        parts.append(predictions(rows, out, fits, test_s))
    pred = pd.concat(parts).join(market_frame(store, rows.game_id.unique()), on="game_id")
    pred = pred[pred.point == "pretip"]
    bt_in = pd.DataFrame({"season": pred.season, "margin": pred.pred,
                          "open_spread": pred.open_spread_home,
                          "close_spread": pred.close_spread_home, "act_margin": pred.margin})
    print("NBA games with model + open + close:", int(bt_in.dropna().shape[0]), "of",
          len(bt_in))
    for label, ss in (("validation 2021-22..2022-23 (fit 2015-16..2020-21)", VALIDATE),
                      ("test 2023-24..2025-26 (fit 2015-16..2022-23)", TEST),
                      ("both", VALIDATE + TEST)):
        x = bt_in[bt_in.season.isin(ss)]
        res = summarize(picks(x, d["spread_threshold"]))
        res["games"] = int(x.dropna().shape[0])
        out_json[f"nba {label}"] = res
        print(f"\n== NBA {label}: games {res['games']}")
        for k in ("all_threshold", "agreement", "no_agreement"):
            print(f"  {k:14s} {res[k]}")
except Exception as e:  # noqa: BLE001
    traceback.print_exc()
    print("NBA backtest failed:", e)

json.dump(out_json, open("out/agreement.json", "w"), indent=1, default=float)
import shutil  # noqa: E402

shutil.rmtree(hist, ignore_errors=True)
