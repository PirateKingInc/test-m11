"""Load the M14 NBA as-of store from the database (MODEL.md, M14 "As-of rules").

Frames (each sorted by `known_at`):
  box       one row per finished game: final score and every player's line, known at
            max(tip + 3 h, the box score's known_at). Post-game facts only.
  injuries  official injury report rows: game_id, team, player_id (matched by name; None if the
            player has no box score yet), status, known_at.
  odds      lines: game_id, kind (open | poll), home spread, home/away moneyline, total.
            An opening line fetched after tip (history backfill) is known at 07:00 ET on game
            day; any other row when it was fetched. Closing lines are not in the store.
`store.games` holds the schedule (known in advance) and `store.closing` the closing lines
(evaluation only, never read by a feature).
"""

from __future__ import annotations

import re
import unicodedata
from datetime import time, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import Connection, select

from betdata.core.matching import sport_id
from betdata.db.schema import (
    books,
    games,
    markets,
    nba_injury_reports,
    nba_player_games,
    odds_snapshots,
    results,
    teams,
)
from betdata.nba.box import RESULT_DELAY, STATS
from betdata.nba.points import EASTERN
from betdata.nfl.store import Frame, Store

# Preferred books for "the" line of a game, best first (history has different books by era).
BOOK_ORDER = ("draftkings", "fanduel", "betmgm", "caesars", "espnbet", "bovada", "betonline",
              "5dimes", "sportsbetting", "westgate", "pointsbet", "unibet", "betrivers",
              "sugarhouse", "opening")  # fmt: skip
OPEN_CLOCK = time(7, 0)  # history: an opening line counts as known at 07:00 ET on game day
SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def norm_name(name: str) -> str:
    """'Porter Jr., Michael' and 'Michael Porter Jr.' -> 'michael porter'."""
    s = str(name)
    if "," in s:
        last, first = s.split(",", 1)
        s = f"{first} {last}"
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]", " ", s.replace("'", ""))
    return " ".join(SUFFIX.sub(" ", s).split())


def _utc(s: pd.Series) -> pd.Series:
    s = pd.to_datetime(s)
    return s.dt.tz_localize("UTC") if s.dt.tz is None else s.dt.tz_convert("UTC")


def load_games(conn: Connection, min_season: int | None = None) -> pd.DataFrame:
    sid = sport_id(conn, "nba")
    h, a = teams.alias("h"), teams.alias("a")
    g = pd.read_sql(
        select(games.c.game_id, games.c.season, games.c.season_type,
               games.c.start_time_utc.label("tip"), games.c.home_team_id.label("home"),
               games.c.away_team_id.label("away"), games.c.neutral_site.label("neutral"),
               games.c.status, h.c.abbrev.label("home_abbr"), a.c.abbrev.label("away_abbr"))
        .join(h, h.c.team_id == games.c.home_team_id)
        .join(a, a.c.team_id == games.c.away_team_id)
        .where(games.c.sport_id == sid, games.c.season_type.in_(("REG", "POST")),
               games.c.status.in_(("final", "scheduled", "in_progress")),
               games.c.season >= (min_season or 0)), conn)  # fmt: skip
    g["tip"] = _utc(g["tip"])
    g["neutral"] = g["neutral"].fillna(False).astype(bool)
    return g.sort_values(["tip", "game_id"]).set_index("game_id", drop=False)


def load_box(conn: Connection, g: pd.DataFrame) -> pd.DataFrame:
    r = pd.read_sql(select(results.c.game_id, results.c.home_score, results.c.away_score)
                    .where(results.c.game_id.in_(select(games.c.game_id).where(
                        games.c.sport_id == sport_id(conn, "nba")))), conn)  # fmt: skip
    r = r[r.game_id.isin(g.index)].drop_duplicates("game_id", keep="last")
    cols = ["team_id", "player_id", "minutes", "points", "plus_minus", "starter", *STATS]
    lo = int(g.season.min()) if len(g) else 0
    p = pd.read_sql(select(nba_player_games.c.game_id, nba_player_games.c.known_at,
                           *(nba_player_games.c[c] for c in cols))
                    .join(games, games.c.game_id == nba_player_games.c.game_id)
                    .where(games.c.season >= lo), conn)  # fmt: skip
    p = p[p.game_id.isin(r.game_id)]
    p["known_at"] = _utc(p["known_at"])
    num = ["minutes", "points", "plus_minus", *STATS]
    p[num] = p[num].astype(float).fillna(0.0)
    p["starter"] = p["starter"].fillna(False).astype(bool)
    known_box = p.groupby("game_id").known_at.max()
    lines = {gid: tuple(map(tuple, grp[cols].itertuples(index=False, name=None)))
             for gid, grp in p.groupby("game_id")}  # fmt: skip
    r["players"] = r.game_id.map(lambda x: lines.get(x, ()))
    tip = r.game_id.map(g["tip"])
    r["known_at"] = np.maximum(tip + RESULT_DELAY, r.game_id.map(known_box).fillna(tip
                                                                                + RESULT_DELAY))
    for c in ("home", "away", "season", "neutral"):
        r[c] = r.game_id.map(g[c])
    return r.reset_index(drop=True)


BOX_COLUMNS = ("team_id", "player_id", "minutes", "points", "plus_minus", "starter", *STATS)


def load_injuries(conn: Connection, g: pd.DataFrame) -> pd.DataFrame:
    t = nba_injury_reports
    i = pd.read_sql(select(t.c.game_id, t.c.team_id, t.c.player_name, t.c.status, t.c.known_at)
                    .where(t.c.game_id.is_not(None)), conn)  # fmt: skip
    i = i[i.game_id.isin(g.index)].copy()
    i["known_at"] = _utc(i["known_at"])
    # Names -> ESPN ids through the players' box-score names (same team first, then anyone).
    names = pd.read_sql(select(nba_player_games.c.player_id, nba_player_games.c.player_name,
                               nba_player_games.c.team_id).distinct(), conn)  # fmt: skip
    names["key"] = names.player_name.map(norm_name)
    by_team = {(r.team_id, r.key): r.player_id for r in names.itertuples()}
    # A name shared by two different players is ambiguous; one player seen on several teams
    # is not (REVIEW.md M14-6).
    ids = names.drop_duplicates(["key", "player_id"])
    anyone = ids.drop_duplicates("key", keep=False).set_index("key").player_id.to_dict()
    keys = i.player_name.map(norm_name)
    i["player_id"] = [by_team.get((tm, k)) or anyone.get(k)
                      for tm, k in zip(i.team_id, keys, strict=True)]  # fmt: skip
    return i[["game_id", "team_id", "player_id", "player_name", "status", "known_at"]]


def load_odds(conn: Connection, g: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(odds frame of open/poll lines, closing lines per game for evaluation)."""
    o = pd.read_sql(
        select(odds_snapshots.c.game_id, books.c.key.label("book"),
               markets.c.key.label("market"), odds_snapshots.c.outcome, odds_snapshots.c.line,
               odds_snapshots.c.price_american.label("price"),
               odds_snapshots.c.snapshot_kind.label("kind"), odds_snapshots.c.fetched_at,
               odds_snapshots.c.price_status)
        .join(books, books.c.book_id == odds_snapshots.c.book_id)
        .join(markets, markets.c.market_id == odds_snapshots.c.market_id)
        .where(odds_snapshots.c.game_id.is_not(None)), conn)  # fmt: skip
    o = o[o.game_id.isin(g.index) & (o.price_status == "active")].copy()
    o["fetched_at"] = _utc(o["fetched_at"])
    o["rank"] = o.book.map({b: i for i, b in enumerate(BOOK_ORDER)}).fillna(len(BOOK_ORDER))
    o = o.sort_values(["game_id", "kind", "rank", "fetched_at"])

    def wide(df: pd.DataFrame, keys) -> pd.DataFrame:
        """One row per keys: home spread, moneylines and total from the best-ranked book."""
        out = {}
        for (m, oc, col, val) in (("spreads", "home", "spread_home", "line"),
                                  ("h2h", "home", "ml_home", "price"),
                                  ("h2h", "away", "ml_away", "price"),
                                  ("totals", "over", "total", "line")):  # fmt: skip
            sub = df[(df.market == m) & (df.outcome == oc) & df[val].notna()]
            out[col] = sub.groupby(keys)[val].first()
        books_used = df.groupby(keys).book.first()
        return pd.DataFrame(out).join(books_used).reset_index()

    tip = o.game_id.map(g["tip"])
    opening = o[o.kind == "source_opening"]
    op = wide(opening, ["game_id"])
    first_seen = opening.groupby("game_id").fetched_at.min()
    known = []
    for gid in op.game_id:
        t, seen = g.at[gid, "tip"], first_seen[gid]
        if seen > t:  # backfilled after the game: the 07:00 ET rule
            day = t.tz_convert(EASTERN).date()
            seen = pd.Timestamp.combine(day, OPEN_CLOCK).tz_localize(EASTERN).tz_convert("UTC")
        known.append(seen)
    op["known_at"] = pd.to_datetime(pd.Series(known, dtype="object"), utc=True) if known \
        else pd.Series([], dtype="datetime64[ns, UTC]")
    op["kind"] = "open"
    poll = o[(o.kind == "poll") & (o.fetched_at < tip)]
    pw = wide(poll, ["game_id", "fetched_at"]).rename(columns={"fetched_at": "known_at"})
    pw["kind"] = "poll"
    frame = pd.concat([op, pw], ignore_index=True)
    frame["known_at"] = _utc(frame["known_at"])
    # Closing: a source's own close, else the last poll before tip (live seasons).
    close = wide(o[o.kind == "source_closing"], ["game_id"]).set_index("game_id")
    last_poll = pw.sort_values("known_at").groupby("game_id").last()
    close = close.combine_first(last_poll[["spread_home", "ml_home", "ml_away", "total",
                                           "book"]])  # fmt: skip
    return frame, close


def load_store(conn: Connection, min_season: int | None = None) -> Store:
    """`min_season`: load only seasons from this one on (the live runner's recent window)."""
    g = load_games(conn, min_season)
    box = load_box(conn, g)
    inj = load_injuries(conn, g)
    odds, close = load_odds(conn, g)
    st = Store(g, {"box": Frame(box), "injuries": Frame(inj), "odds": Frame(odds)})
    st.params["closing"] = close  # evaluation only; no feature reads it
    names = pd.read_sql(select(nba_player_games.c.player_id, nba_player_games.c.player_name)
                        .distinct(), conn)  # fmt: skip
    st.names.update(dict(zip(names.player_id, names.player_name, strict=True)))
    st.names.update({f"team:{r.home}": r.home_abbr for r in g.itertuples()})
    st.names.update({f"team:{r.away}": r.away_abbr for r in g.itertuples()})
    return st


def outcomes(store: Store) -> pd.DataFrame:
    """Final scores, for fitting and scoring only (never a feature)."""
    b = store.frame("box").df.set_index("game_id")
    return pd.DataFrame({"margin": b.home_score - b.away_score,
                         "total": b.home_score + b.away_score})  # fmt: skip


__all__ = ["BOX_COLUMNS", "load_store", "norm_name", "outcomes", "timedelta"]
