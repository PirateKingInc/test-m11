"""Read-only diagnostic (M17 sign audit): NFL graded picks since 2026-10-15 with every line
stored for those games, per source and book, in both the stored and the game orientation.
Prints and writes CSVs under out/. No secrets are read or printed."""

import sqlite3
from pathlib import Path

import pandas as pd

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)
pd.set_option("display.max_rows", 2000)
OUT = Path("out")
OUT.mkdir(exist_ok=True)
db = sqlite3.connect("data/betdata.sqlite")
q = lambda s: pd.read_sql_query(s, db)  # noqa: E731

grades = q("""
SELECT g.grade_id, g.model_version, g.market, g.side, g.line, g.price_american, g.edge,
       g.close_line, g.result, g.profit, g.graded_at,
       p.prediction_id, p.predicted_at, p.prediction_point, p.pred_home_margin,
       p.pred_home_win_prob, p.market_spread_home, p.market_source, p.market_ml_home,
       p.market_ml_away, p.blend_home_margin,
       gm.game_id, gm.start_time_utc, gm.week, gm.neutral_site, ht.abbrev AS home, at.abbrev AS away
FROM nfl_bet_grades g
JOIN model_predictions p ON p.prediction_id = g.prediction_id
JOIN games gm ON gm.game_id = g.game_id
JOIN teams ht ON ht.team_id = gm.home_team_id
JOIN teams at ON at.team_id = gm.away_team_id
WHERE gm.start_time_utc >= '2026-10-15'
ORDER BY gm.start_time_utc, g.model_version, g.market
""")
ids = ",".join(str(i) for i in sorted(set(grades.game_id))) or "-1"
res = q(f"SELECT game_id, source, home_score, away_score, final, updated_at FROM results "
        f"WHERE game_id IN ({ids}) ORDER BY game_id, source")
odds = q(f"""
SELECT v.game_id, v.source, b.key AS book, m.key AS market, v.outcome, v.game_outcome,
       v.line, v.price_american, v.snapshot_kind, v.price_status, v.fetched_at,
       v.source_game_id, gs.home_away_swapped, se.home_name AS src_home, se.away_name AS src_away
FROM v_odds_snapshots v
JOIN markets m ON m.market_id = v.market_id
JOIN books b ON b.book_id = v.book_id
LEFT JOIN game_source_ids gs ON gs.source = v.source AND gs.source_game_id = v.source_game_id
LEFT JOIN source_events se ON se.source = v.source AND se.source_game_id = v.source_game_id
WHERE v.game_id IN ({ids}) AND m.key IN ('spreads', 'h2h') AND m.period = 'game'
ORDER BY v.game_id, m.key, v.source, b.key, v.fetched_at, v.outcome
""")
gsi = q(f"SELECT * FROM game_source_ids WHERE game_id IN ({ids}) ORDER BY game_id, source")
for name, df in (("grades", grades), ("results", res), ("odds", odds), ("game_source_ids", gsi)):
    df.to_csv(OUT / f"{name}.csv", index=False)

print("== versions graded since Oct 15 (market: W-L-P)")
for (v, mk), d in grades.groupby(["model_version", "market"]):
    print(f"{v:40s} {mk:9s} {(d.result == 'win').sum()}-{(d.result == 'loss').sum()}"
          f"-{(d.result == 'push').sum()}  void {(d.result == 'void').sum()}")
print("\n== grades (spread + moneyline)")
print(grades[grades.market.isin(["spread", "moneyline"])][[
    "model_version", "start_time_utc", "away", "home", "neutral_site", "market", "side", "line",
    "price_american", "edge", "pred_home_margin", "pred_home_win_prob", "market_spread_home",
    "market_ml_home", "market_ml_away", "predicted_at", "prediction_point", "result"]]
      .to_string(index=False))
print("\n== results")
print(res.to_string(index=False))
print("\n== game_source_ids")
print(gsi.to_string(index=False))
print("\n== latest polled spread per game/source/book/outcome before kickoff")
k = odds.merge(grades[["game_id", "start_time_utc"]].drop_duplicates(), on="game_id")
k = k[(k.fetched_at <= k.start_time_utc) & (k.snapshot_kind == "poll")]
last = k.sort_values("fetched_at").groupby(
    ["game_id", "market", "source", "book", "outcome"]).tail(1)
print(last[["game_id", "market", "source", "book", "outcome", "game_outcome", "line",
            "price_american", "fetched_at", "home_away_swapped", "src_home", "src_away"]]
      .sort_values(["game_id", "market", "source", "book", "outcome"]).to_string(index=False))
print("\n== first fetch per source (all NFL odds)")
print(q("""SELECT v.source, MIN(v.fetched_at) first, MAX(v.fetched_at) last, COUNT(*) n
           FROM odds_snapshots v JOIN games g ON g.game_id = v.game_id
           JOIN sports s ON s.sport_id = g.sport_id WHERE s.key = 'nfl'
           GROUP BY v.source""").to_string(index=False))

pdb = Path("data/poller.sqlite")
if pdb.exists():
    pc = sqlite3.connect(pdb)
    tabs = [r[0] for r in pc.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    print("\n== poller tables", tabs)
    for t in tabs:
        cols = [r[1] for r in pc.execute(f"PRAGMA table_info({t})")]
        if "sport" in cols:
            d = pd.read_sql_query(f"SELECT * FROM {t} WHERE sport = 'nfl'", pc)
            d.to_csv(OUT / f"poller_{t}.csv", index=False)
            print(f"\n-- {t} (nfl rows: {len(d)}) columns {cols}")
            print(d.head(60).to_string(index=False))
