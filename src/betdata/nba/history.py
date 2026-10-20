"""NBA history that can only be fetched from GitHub Actions (injury reports, historical lines):
exported there as gzipped CSV keyed by ESPN game id, and loaded into any database that has the
NBA schedule (no network). The `nba-history` workflow publishes the files on the `nba-history`
branch; `betdata nba load-history <dir>` loads them. The live runner does not need them: they
feed the backtest (fitting, validation, the final test).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
from sqlalchemy import Connection, Engine, insert, select

from betdata.core.matching import sport_id
from betdata.db.schema import (
    books,
    game_source_ids,
    games,
    markets,
    nba_injury_reports,
    odds_snapshots,
)
from betdata.nba.box import espn_games

log = logging.getLogger(__name__)
ODDS_FILE, INJURY_FILE = "odds_{season}.csv.gz", "injuries_{season}.csv.gz"
INJURY_COLS = ("game_date", "matchup", "team_name", "player_name", "status", "reason",
               "report_at", "known_at", "source", "fetched_at")  # fmt: skip


def _espn_of(conn: Connection) -> dict[int, str]:
    return {g: e for e, g in conn.execute(select(game_source_ids.c.source_game_id,
                                                 game_source_ids.c.game_id)
                                          .where(game_source_ids.c.source == "espn")).all()}


def export(engine: Engine, out: Path, season: int) -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    with engine.connect() as conn:
        sid = sport_id(conn, "nba")
        espn = _espn_of(conn)
        o = pd.read_sql(
            select(odds_snapshots.c.game_id, books.c.key.label("book"),
                   markets.c.key.label("market"), odds_snapshots.c.outcome,
                   odds_snapshots.c.line, odds_snapshots.c.price_american,
                   odds_snapshots.c.snapshot_kind, odds_snapshots.c.fetched_at,
                   odds_snapshots.c.source)
            .join(books, books.c.book_id == odds_snapshots.c.book_id)
            .join(markets, markets.c.market_id == odds_snapshots.c.market_id)
            .join(games, games.c.game_id == odds_snapshots.c.game_id)
            .where(games.c.sport_id == sid, games.c.season == season,
                   odds_snapshots.c.snapshot_kind != "poll"), conn)  # fmt: skip
        i = pd.read_sql(select(nba_injury_reports), conn)
    o["espn_game_id"] = o.pop("game_id").map(espn)
    o.to_csv(out / ODDS_FILE.format(season=season), index=False)
    i = i[list(INJURY_COLS)]
    i.to_csv(out / INJURY_FILE.format(season=season), index=False)
    return {"odds": len(o), "injuries": len(i)}


def load(engine: Engine, src: Path) -> dict[str, int]:
    """Load every exported file in `src` (idempotent: a game's or report's rows already present
    from the same source are skipped)."""
    from betdata.core.normalize.catalog import BookResolver, MarketResolver
    from betdata.core.normalize.teams import TeamResolver
    from betdata.sources.nba_injury import _games_index

    counts = {"odds": 0, "injuries": 0}
    with engine.begin() as conn:
        for f in sorted(src.glob("odds_*.csv.gz")):
            df = pd.read_csv(f, dtype={"espn_game_id": str})
            ids = espn_games(conn, df.espn_game_id.dropna().unique())
            have = set(conn.execute(select(odds_snapshots.c.game_id).where(
                odds_snapshots.c.snapshot_kind != "poll",
                odds_snapshots.c.source == "espn_nba").distinct()).scalars())  # fmt: skip
            df = df[df.espn_game_id.map(ids).notna()]
            df = df[~df.espn_game_id.map(ids).isin(have)]
            bk, mk = BookResolver(conn, "espn_nba"), MarketResolver(conn)
            rows = [dict(source=r.source, source_game_id=r.espn_game_id,
                         game_id=ids[r.espn_game_id], book_id=bk.resolve(r.book),
                         market_id=mk.resolve(r.market), outcome=r.outcome,
                         line=None if pd.isna(r.line) else float(r.line),
                         price_american=None if pd.isna(r.price_american)
                         else int(r.price_american), price_status="active",
                         snapshot_kind=r.snapshot_kind,
                         fetched_at=pd.Timestamp(r.fetched_at).to_pydatetime())
                    for r in df.itertuples()]  # fmt: skip
            for k in range(0, len(rows), 1000):
                conn.execute(insert(odds_snapshots), rows[k:k + 1000])
            counts["odds"] += len(rows)
        resolver = TeamResolver(conn, sport_id(conn, "nba"))
        for f in sorted(src.glob("injuries_*.csv.gz")):
            df = pd.read_csv(f, keep_default_na=False, na_values=[""])
            if df.empty:
                continue
            for c in ("report_at", "known_at", "fetched_at"):
                df[c] = pd.to_datetime(df[c], utc=True, format="mixed")
            have = set(conn.execute(select(nba_injury_reports.c.game_date,
                                           nba_injury_reports.c.matchup,
                                           nba_injury_reports.c.player_name,
                                           nba_injury_reports.c.report_at)).all())
            have = {(a, b, c, pd.Timestamp(d).tz_convert("UTC")) for a, b, c, d in have}
            ix = _games_index(conn, set(df.game_date.unique()))
            rows = []
            for r in df.itertuples():
                if (r.game_date, r.matchup, r.player_name, r.report_at) in have:
                    continue
                rows.append(dict(
                    game_date=r.game_date, matchup=r.matchup, team_name=r.team_name,
                    game_id=ix.get((r.game_date, r.matchup)),
                    team_id=resolver.lookup("nba_injury", r.team_name),
                    player_name=r.player_name, status=r.status,
                    reason=None if pd.isna(r.reason) else r.reason,
                    report_at=r.report_at.to_pydatetime(), known_at=r.known_at.to_pydatetime(),
                    source=r.source, fetched_at=r.fetched_at.to_pydatetime()))  # fmt: skip
            for k in range(0, len(rows), 1000):
                conn.execute(insert(nba_injury_reports), rows[k:k + 1000])
            counts["injuries"] += len(rows)
    return counts
