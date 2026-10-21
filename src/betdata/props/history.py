"""Player prop line history (ESPN, M15), fetched on GitHub Actions only: exported there as one
gzipped CSV per sport and season, keyed by ESPN game id plus kickoff and team names (NFL
schedules carry no ESPN id), and loaded into any database with that schedule (no network).
The `props-history` workflow publishes the files on the `props-history` branch.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, insert, select

from betdata.core.matching import sport_id
from betdata.core.normalize.catalog import BookResolver
from betdata.db.schema import books, game_source_ids, games, prop_lines, teams
from betdata.sources.espn_props import NAME, espn_ids, match_events, record_nfl_ids

FILE = "props_{sport}_{season}.csv.gz"
COLS = ("espn_game_id", "start_time_utc", "home", "away", "player_id", "player_name", "market",
        "book", "kind", "line", "over_price", "under_price", "quoted_at", "fetched_at")  # fmt: skip


def export(engine: Engine, out: Path, sport: str, season: int) -> int:
    out.mkdir(parents=True, exist_ok=True)
    h, a = teams.alias("h"), teams.alias("a")
    with engine.connect() as conn:
        df = pd.read_sql(
            select(prop_lines.c.source_game_id.label("espn_game_id"), games.c.start_time_utc,
                   h.c.canonical_name.label("home"), a.c.canonical_name.label("away"),
                   prop_lines.c.player_id, prop_lines.c.player_name, prop_lines.c.market,
                   books.c.key.label("book"), prop_lines.c.kind, prop_lines.c.line,
                   prop_lines.c.over_price, prop_lines.c.under_price, prop_lines.c.quoted_at,
                   prop_lines.c.fetched_at)
            .join(games, games.c.game_id == prop_lines.c.game_id)
            .join(h, h.c.team_id == games.c.home_team_id)
            .join(a, a.c.team_id == games.c.away_team_id)
            .join(books, books.c.book_id == prop_lines.c.book_id)
            .where(games.c.sport_id == sport_id(conn, sport), games.c.season == season,
                   prop_lines.c.source == NAME, prop_lines.c.kind != "poll"), conn)  # fmt: skip
    df[list(COLS)].to_csv(out / FILE.format(sport=sport, season=season), index=False)
    return len(df)


def _ts(v):
    return None if pd.isna(v) else pd.Timestamp(v).tz_convert("UTC").to_pydatetime()


def load(engine: Engine, src: Path) -> dict[str, int]:
    """Load every exported file in `src`. Idempotent: games that already have history rows
    are skipped. Games are found by ESPN id, else by kickoff and teams (NFL ids are recorded
    as `espn_nfl`)."""
    counts: dict[str, int] = {}
    with engine.begin() as conn:
        for f in sorted(src.glob("props_*_*.csv.gz")):
            sport = f.name.split("_")[1]
            df = pd.read_csv(f, dtype={"espn_game_id": str, "player_id": str})
            for c in ("start_time_utc", "quoted_at", "fetched_at"):
                df[c] = pd.to_datetime(df[c], utc=True, format="mixed")
            have = set(conn.execute(select(prop_lines.c.source_game_id).where(
                prop_lines.c.source == NAME, prop_lines.c.kind != "poll")).scalars())
            df = df[~df.espn_game_id.isin(have)]
            events = [{"id": e, "date": g.start_time_utc.iloc[0].isoformat(),
                       "home": [g.home.iloc[0]], "away": [g.away.iloc[0]]}
                      for e, g in df.groupby("espn_game_id")]  # fmt: skip
            if sport == "nfl":
                record_nfl_ids(conn, events)
            ids = espn_ids(conn, sport, game_source_ids.c.source_game_id.in_(
                [e["id"] for e in events]))  # fmt: skip
            ids |= {e: g for e, (g, _) in match_events(conn, sport, events).items()
                    if e not in ids}  # fmt: skip
            bk = BookResolver(conn, NAME)
            rows = [dict(source=NAME, source_game_id=r.espn_game_id,
                         game_id=ids.get(r.espn_game_id), player_id=r.player_id,
                         player_name=None if pd.isna(r.player_name) else r.player_name,
                         market=r.market, book_id=bk.resolve(r.book), kind=r.kind,
                         line=None if pd.isna(r.line) else float(r.line),
                         over_price=None if pd.isna(r.over_price) else int(r.over_price),
                         under_price=None if pd.isna(r.under_price) else int(r.under_price),
                         quoted_at=_ts(r.quoted_at), fetched_at=_ts(r.fetched_at))
                    for r in df.itertuples()]  # fmt: skip
            for k in range(0, len(rows), 1000):
                conn.execute(insert(prop_lines), rows[k:k + 1000])
            counts[f.name] = len(rows)
    return counts
