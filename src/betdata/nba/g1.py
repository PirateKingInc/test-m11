"""M18 Part G1: early-season NBA totals (MODEL.md "M18 Part G"; Baryla, Borghesi, Dare &
Dennis 2007).

**Rule:** bet the under in each team's first four home games of a regular season when the
closing total is at least 0.5 points below the opening total.

**Backtest:** every stored season with an opening and a closing total.
- **Lines:** one book per game, the first in BOOK_ORDER with both, from ESPN's history (source
  open/close rows) and polled lines.
- **Grading:** at the closing total and under price; -110 when the history has no price.
- **Reported:** n, W-L-P, the win rate's 95% Wilson range, ROI.

**Live:** paper-traded for the rest of this season. The poller's `g1_check` alerts the best
DK/FD under ten minutes before tip; the open is the first stored quote.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from sqlalchemy import Connection, select

from betdata.core.matching import sport_id
from betdata.db.schema import books, games, markets, odds_snapshots, results

HOME_GAMES = 4
DROP = 0.5
BOOK_ORDER = ("draftkings", "fanduel", "espnbet", "caesars", "betmgm", "consensus", "opening")
REPORT = Path("reports/nba/g1_backtest.json")


def early_home_games(conn: Connection, season: int | None = None) -> pd.DataFrame:
    """game_id, season, tip, home_team_id, n (its home game number in the regular season)."""
    q = (select(games.c.game_id, games.c.season, games.c.start_time_utc.label("tip"),
                games.c.home_team_id)
         .where(games.c.sport_id == sport_id(conn, "nba"), games.c.season_type == "REG",
                games.c.status.notin_(("postponed", "cancelled"))))  # fmt: skip
    if season is not None:
        q = q.where(games.c.season == season)
    g = pd.read_sql(q, conn)
    if g.empty:
        return g.assign(n=pd.Series(dtype=int))
    g = g.sort_values("tip")
    g["n"] = g.groupby(["season", "home_team_id"]).cumcount() + 1
    return g[g.n <= HOME_GAMES]


def open_close(conn: Connection, game_ids) -> pd.DataFrame:
    """Per game: the opening and closing total and the closing under price of one book."""
    s = odds_snapshots
    df = pd.read_sql(
        select(s.c.game_id, books.c.key.label("book"), s.c.outcome, s.c.line,
               s.c.price_american, s.c.snapshot_kind, s.c.fetched_at)
        .join(books, books.c.book_id == s.c.book_id)
        .join(markets, markets.c.market_id == s.c.market_id)
        .where(s.c.game_id.in_([int(x) for x in game_ids]), markets.c.key == "totals",
               markets.c.period == "game", s.c.line.is_not(None)), conn)  # fmt: skip
    if df.empty:
        return pd.DataFrame(columns=["game_id", "book", "open", "close", "under_price"])
    df = df.sort_values("fetched_at")
    out = []
    rank = {b: i for i, b in enumerate(BOOK_ORDER)}
    for (gid, book), d in df.groupby(["game_id", "book"]):
        op = d[d.snapshot_kind == "source_opening"]
        cl = d[d.snapshot_kind == "source_closing"]
        polls = d[d.snapshot_kind == "poll"]
        o = op.line.iloc[0] if len(op) else (polls.line.iloc[0] if len(polls) else None)
        c_rows = cl if len(cl) else polls
        if o is None or c_rows.empty:
            continue
        c = c_rows.line.iloc[-1]
        under = c_rows[(c_rows.outcome == "under") & (c_rows.line == c)]
        price = under.price_american.iloc[-1] if len(under) else None
        out.append(dict(game_id=gid, book=book, open=float(o), close=float(c),
                        under_price=None if price is None or pd.isna(price) else int(price),
                        rank=rank.get(book, len(rank))))  # fmt: skip
    r = pd.DataFrame(out)
    if r.empty:
        return pd.DataFrame(columns=["game_id", "book", "open", "close", "under_price"])
    return r.sort_values("rank").drop_duplicates("game_id").drop(columns="rank")


def picks(conn: Connection, season: int | None = None) -> pd.DataFrame:
    g = early_home_games(conn, season)
    if g.empty:
        return g
    oc = open_close(conn, g.game_id)
    d = g.merge(oc, on="game_id", how="inner")
    d = d[d.close <= d.open - DROP].copy()
    res = pd.read_sql(select(results.c.game_id, results.c.home_score, results.c.away_score)
                      .where(results.c.game_id.in_([int(x) for x in d.game_id]),
                             results.c.final.is_(True)), conn)  # fmt: skip
    res = res.drop_duplicates("game_id")
    d = d.merge(res, on="game_id", how="left")
    d["total"] = d.home_score + d.away_score
    d["result"] = [None if pd.isna(t) else "win" if t < c else "loss" if t > c else "push"
                   for t, c in zip(d.total, d.close, strict=True)]  # fmt: skip
    return d


def record(d: pd.DataFrame) -> dict:
    from betdata.moneyline import profit
    from betdata.nba.selection import wilson

    d = d[d.result.notna()]
    w, lo, pu = (int((d.result == x).sum()) for x in ("win", "loss", "push"))
    prof = [profit(r, -110 if p is None or pd.isna(p) else int(p))
            for r, p in zip(d.result, d.under_price, strict=True)]  # fmt: skip
    ci = wilson(w, w + lo) if w + lo else (None, None)
    return {"n": int(len(d)), "w": w, "l": lo, "p": pu,
            "win_rate": round(w / (w + lo), 4) if w + lo else None,
            "win_rate_95": [round(ci[0], 4), round(ci[1], 4)] if w + lo else None,
            "roi": round(sum(prof) / len(d), 4) if len(d) else None,
            "breakeven_at_110": 0.5238}  # fmt: skip


def backtest(conn: Connection) -> dict:
    d = picks(conn)
    out = {"rule": f"under, each team's first {HOME_GAMES} home games, close <= open - {DROP}",
           "books": list(BOOK_ORDER), "all": record(d) if len(d) else None, "by_season": {},
           "candidates_with_lines": None}  # fmt: skip
    if len(d):
        for s, g in d.groupby("season"):
            out["by_season"][int(s)] = record(g)
    g = early_home_games(conn)
    if len(g):
        oc = open_close(conn, g.game_id)
        out["candidates_with_lines"] = int(g.game_id.isin(oc.game_id).sum())
        out["early_home_games"] = int(len(g))
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(out, indent=1, default=float) + "\n")
    return out
