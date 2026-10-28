"""M17 Part D: the betting lines the engine is compared with, read only after it has predicted
(never an input). Offline, from stored files:

  closing   nflverse's closing spread and total per game (`odds_snapshots`, source_closing)
  espn      ESPN's per-book opening and closing lines (the `m17-data` branch, fetched on
            Actions by `betdata nfl engine fetch lines`), matched to nflverse games by date and
            teams; one consensus book per game (the first listed with both)
  props     ESPN BET opening and closing player prop lines 2024-2025 (the `props-history`
            branch), ESPN athlete ids mapped to gsis ids through nflverse `players`
"""

from __future__ import annotations

import gzip
import io
import json
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

# ESPN lists projection sites beside the books; their numbers are not lines
NOT_BOOKS = {"consensus", "numberfire", "teamrankings", "accuscore", "sportsinsights"}
# ESPN abbreviation -> nflverse
ESPN_TEAM = {"WSH": "WAS", "LAR": "LA", "JAC": "JAX", "OAK": "LV", "SD": "LAC", "STL": "LA"}


def closing(engine=None) -> pd.DataFrame:
    """nflverse game id -> close_spread_home (negative = home favoured), close_total."""
    from sqlalchemy import text

    from betdata.nfl.cli import _engine

    engine = engine or _engine()
    q = text("""select o.source_game_id g, m.key k, o.outcome, o.line from odds_snapshots o
                join markets m on m.market_id = o.market_id
                where o.source = 'nflverse' and o.snapshot_kind = 'source_closing'
                and ((m.key = 'spreads' and o.outcome = 'home')
                     or (m.key = 'totals' and o.outcome = 'over'))""")
    with engine.connect() as c:
        df = pd.DataFrame(c.execute(q).all(), columns=["game_id", "k", "o", "line"])
    df = df.drop_duplicates(["game_id", "k"], keep="last")
    w = df.pivot(index="game_id", columns="k", values="line")
    return w.rename(columns={"spreads": "close_spread_home", "totals": "close_total"})


def _git_file(ref: str, path: str) -> bytes | None:
    try:
        return subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True,
                              check=True).stdout  # fmt: skip
    except subprocess.CalledProcessError:
        return None


def _nflverse_games(ids: pd.DataFrame, store) -> pd.DataFrame:
    tg = store.team_games.drop_duplicates("game_id").set_index("game_id")
    g = ids.join(tg[["kickoff"]], how="inner")
    g["date"] = g.kickoff.dt.tz_convert("America/New_York").dt.date
    return g.reset_index()


def espn(store, ids: pd.DataFrame, seasons, ref: str = "origin/m17-data",
         local: Path | None = None) -> pd.DataFrame:  # fmt: skip
    """Per nflverse game: open/close home spread and total from ESPN's books."""
    rows = []
    for s in seasons:
        name = f"nfl_lines_{s}.json"
        body = (local / name).read_bytes() if local and (local / name).exists() else _git_file(
            ref, name)
        if not body:
            continue
        for g in json.loads(body):
            for b in g.get("books") or []:
                rows.append(dict(espn_id=g["espn_id"], date=g["date"], home=g["home"],
                                 away=g["away"], provider=b["provider"],
                                 close_spread=_f(b.get("spread")),
                                 open_spread=_f(b.get("open_spread")),
                                 close_total=_f(b.get("total")),
                                 open_total=_f(b.get("open_total"))))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df[~df.provider.str.lower().str.replace(" ", "").isin(NOT_BOOKS)]
    df["home"] = df.home.replace(ESPN_TEAM)
    df["away"] = df.away.replace(ESPN_TEAM)
    df["date"] = pd.to_datetime(df.date, utc=True).dt.tz_convert("America/New_York").dt.date
    df = df[df.close_spread.notna() & df.open_spread.notna()]
    df = df.drop_duplicates("espn_id", keep="first")  # first listed book with both lines
    games = _nflverse_games(ids, store)
    m = df.merge(games, on=["home", "away", "date"], how="inner")
    return m.set_index("game_id")[["provider", "open_spread", "close_spread", "open_total",
                                   "close_total"]]  # fmt: skip


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return np.nan


def props(store, ids: pd.DataFrame, seasons=(2024, 2025), ref: str = "origin/props-history"):
    """ESPN BET prop lines: nflverse game id, gsis, market, open/close line and prices."""
    from betdata.nfl.engine.plays import root
    from betdata.nfl.engine.web import team_name

    frames = []
    for s in seasons:
        body = _git_file(ref, f"props_nfl_{s}.csv.gz")
        if body:
            frames.append(pd.read_csv(io.BytesIO(gzip.decompress(body))))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    pl = pd.read_parquet(root() / "nflverse" / "players" / "players.parquet",
                         columns=["gsis_id", "espn_id"]).dropna()  # fmt: skip
    pl["espn_id"] = pl.espn_id.astype(str).str.replace(r"\.0$", "", regex=True)
    df["player_id"] = df.player_id.astype(str)
    df = df.merge(pl.drop_duplicates("espn_id"), left_on="player_id", right_on="espn_id",
                  how="left")  # fmt: skip
    df["date"] = pd.to_datetime(df.start_time_utc, utc=True).dt.tz_convert(
        "America/New_York").dt.date
    games = _nflverse_games(ids, store)
    games = games[games.season.isin(list(seasons))]
    games["home_name"] = [team_name(h, int(s)) for h, s in zip(games.home, games.season,
                                                               strict=True)]
    df = df.merge(games[["game_id", "home_name", "date"]], left_on=["home", "date"],
                  right_on=["home_name", "date"], how="inner")  # fmt: skip
    w = df.pivot_table(index=["game_id", "gsis_id", "market"], columns="kind",
                       values=["line", "over_price", "under_price"], aggfunc="first")
    w.columns = [f"{k}_{v}" for v, k in w.columns]
    return w.reset_index()
