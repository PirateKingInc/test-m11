"""Before/after of the DK/FD regrade on a copy of the hourly state (never saved)."""

from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import select

from betdata.config import get_settings
from betdata.db.engine import make_engine, upgrade
from betdata.db.schema import games, model_predictions, nfl_bet_grades, teams

pd.set_option("display.width", 250)
pd.set_option("display.max_rows", 500)
pd.set_option("display.max_columns", 30)


def counted(conn, with_books):
    """The page's rule: one pick per (version, market, game), the latest graded prediction."""
    g, mp, h, a = nfl_bet_grades, model_predictions, teams.alias("h"), teams.alias("a")
    cols = [g.c.model_version, g.c.market, g.c.game_id, g.c.side, g.c.line, g.c.price_american,
            g.c.edge, g.c.close_line, g.c.close_price_american, g.c.clv, g.c.result, g.c.profit,
            mp.c.predicted_at, mp.c.prediction_point, mp.c.pred_home_margin,
            mp.c.pred_home_win_prob, games.c.start_time_utc, h.c.abbrev.label("home"),
            a.c.abbrev.label("away")]
    if with_books:
        cols += [g.c.book, g.c.close_book]
    q = (select(*cols).join(mp, mp.c.prediction_id == g.c.prediction_id)
         .join(games, games.c.game_id == g.c.game_id)
         .join(h, h.c.team_id == games.c.home_team_id)
         .join(a, a.c.team_id == games.c.away_team_id))
    d = pd.DataFrame(conn.execute(q).mappings().all())
    if d.empty:
        return d
    d = d.sort_values("predicted_at").groupby(["model_version", "market", "game_id"]).tail(1)
    return d.sort_values(["model_version", "market", "start_time_utc"])


def summary(d):
    if d.empty:
        return pd.DataFrame()
    out = []
    for (v, m), x in d.groupby(["model_version", "market"]):
        n = x[x.result.isin(["win", "loss", "push"])]
        clv = n.clv.dropna()
        out.append(dict(version=v, market=m, W=(x.result == "win").sum(),
                        L=(x.result == "loss").sum(), P=(x.result == "push").sum(),
                        V=(x.result == "void").sum(), units=round(x.profit.sum(), 2),
                        roi=round(x.profit.sum() / len(n), 3) if len(n) else None,
                        avg_clv=round(clv.mean(), 2) if len(clv) else None, clv_n=len(clv)))
    return pd.DataFrame(out)


url = get_settings().db_url
eng = make_engine(url)
with eng.connect() as c:
    before = counted(c, with_books=False)
print("== BEFORE (as stored)")
print(summary(before).to_string(index=False))
upgrade(url)  # 0019 on this copy only
from betdata.nfl.grade import grade_nfl  # noqa: E402

with eng.begin() as c:
    run = grade_nfl(c, datetime.now(UTC))
print("\nregrade:", run)
with eng.connect() as c:
    after = counted(c, with_books=True)
print("\n== AFTER (DK/FD best of two at pick time; CLV vs Pinnacle, else DK/FD close)")
print(summary(after).to_string(index=False))
show = ["start_time_utc", "away", "home", "prediction_point", "pred_home_margin",
        "pred_home_win_prob", "side", "book", "line", "price_american", "edge", "close_book",
        "close_line", "close_price_american", "clv", "result"]
for v in sorted(after.model_version.unique()) if len(after) else []:
    for m in ("spread", "moneyline", "total"):
        x = after[(after.model_version == v) & (after.market == m)]
        if len(x):
            print(f"\n-- after: {v} {m}")
            print(x[show].to_string(index=False))
for m in ("spread", "moneyline") if len(before) else ():
    x = before[(before.model_version.str.startswith("m13w")) & (before.market == m)]
    print(f"\n-- before: m13w {m}")
    print(x[[c for c in show if c in x.columns]].to_string(index=False))
