"""`betdata lines coverage` (issue #157): which lines are stored for recent and upcoming games.

For every game that started in the last N days or starts in the next two, per source and book:
- **open**: a source's own opening line (`source_opening`), else the first stored poll;
- **current**: a polled line (snapshots are change-only, so the latest row is the line until
  the next change);
- **close** (started games): a source's own closing line (`source_closing`), else the last poll
  fetched at or before tip (the `v_closing_lines` rule).

Prop lines (`prop_lines`): open (`open` row or first poll), close (a `close` row quoted before
tip, REVIEW M15-3, or a poll fetched at or before tip) and the number of poll rows.

"last poll" is the median minutes between a source's last successful run at or before tip and
the tip: a close is only as fresh as the polling behind it. Read-only.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import Connection, select

from betdata.core.matching import sport_id
from betdata.db.schema import (
    books,
    games,
    ingest_runs,
    markets,
    odds_snapshots,
    prop_lines,
    teams,
)

MARKETS = ("spreads", "h2h", "totals")
AHEAD = timedelta(days=2)
_CHUNK = 500


@dataclass
class Row:
    games: int = 0
    open: int = 0
    current: int = 0
    started: int = 0  # started games with any row: the close's denominator
    close: int = 0
    polls: int = 0  # props: stored poll rows
    tip_gaps: list[float] = field(default_factory=list)  # minutes, last ok run -> tip


@dataclass
class Coverage:
    sport: str
    since: datetime
    until: datetime
    started: list = field(default_factory=list)  # (game_id, start, label)
    upcoming: list = field(default_factory=list)
    lines: dict[tuple[str, str, str], Row] = field(default_factory=dict)
    props: dict[tuple[str, str], Row] = field(default_factory=dict)
    no_close: dict[str, list[str]] = field(default_factory=dict)  # market -> started games
    no_current: dict[str, list[str]] = field(default_factory=dict)  # market -> upcoming games

    @property
    def complete(self) -> bool:
        return not any(self.no_close.values()) and not any(self.no_current.values())


def _games(conn: Connection, sport: str, since: datetime, until: datetime) -> list:
    home, away = teams.alias("h"), teams.alias("a")
    q = (select(games.c.game_id, games.c.start_time_utc, home.c.abbrev.label("home"),
                away.c.abbrev.label("away"))
         .join(home, home.c.team_id == games.c.home_team_id)
         .join(away, away.c.team_id == games.c.away_team_id)
         .where(games.c.sport_id == sport_id(conn, sport),
                games.c.start_time_utc.between(since, until),
                games.c.status.notin_(("postponed", "cancelled")))
         .order_by(games.c.start_time_utc))  # fmt: skip
    return conn.execute(q).all()


def _rows(conn: Connection, query, id_col, ids: list[int]):
    for i in range(0, len(ids), _CHUNK):
        yield from conn.execute(query.where(id_col.in_(ids[i : i + _CHUNK])))


def _run_times(conn: Connection, sources: set[str]) -> dict[str, list[datetime]]:
    out: dict[str, list[datetime]] = defaultdict(list)
    q = (select(ingest_runs.c.source, ingest_runs.c.started_at)
         .where(ingest_runs.c.source.in_(sorted(sources)), ingest_runs.c.status == "ok")
         .order_by(ingest_runs.c.started_at))  # fmt: skip
    for r in conn.execute(q):
        out[r.source].append(r.started_at)
    return out


def _tip_gap(runs: list[datetime], tip: datetime) -> float | None:
    i = bisect_right(runs, tip)
    if i == 0 or tip - runs[i - 1] > timedelta(days=1):
        return None
    return (tip - runs[i - 1]).total_seconds() / 60


def coverage(conn: Connection, sport: str, *, now: datetime, days: int) -> Coverage:
    cov = Coverage(sport, now - timedelta(days=days), now + AHEAD)
    gs = _games(conn, sport, cov.since, cov.until)
    tip = {g.game_id: g.start_time_utc for g in gs}
    label = {g.game_id: f"{g.start_time_utc:%m-%d %H:%M} {g.away}@{g.home}" for g in gs}
    for g in gs:
        (cov.started if g.start_time_utc <= now else cov.upcoming).append(g.game_id)
    ids = list(tip)

    # (game, source, book, market) -> flags
    seen: dict[tuple, dict[str, bool]] = defaultdict(lambda: dict(open=False, current=False,
                                                                  close=False))  # fmt: skip
    q = (select(odds_snapshots.c.game_id, odds_snapshots.c.source, books.c.key.label("book"),
                markets.c.key.label("market"), odds_snapshots.c.snapshot_kind,
                odds_snapshots.c.fetched_at)
         .join(books, books.c.book_id == odds_snapshots.c.book_id)
         .join(markets, markets.c.market_id == odds_snapshots.c.market_id)
         .where(markets.c.period == "game", markets.c.key.in_(MARKETS)))  # fmt: skip
    for r in _rows(conn, q, odds_snapshots.c.game_id, ids):
        f = seen[(r.game_id, r.source, r.book, r.market)]
        f["open"] |= r.snapshot_kind in ("source_opening", "poll")
        f["current"] |= r.snapshot_kind == "poll" and r.fetched_at <= now
        f["close"] |= r.snapshot_kind == "source_closing" or (
            r.snapshot_kind == "poll" and r.fetched_at <= tip[r.game_id])

    runs = _run_times(conn, {k[1] for k in seen})
    for (gid, source, book, market), f in seen.items():
        row = cov.lines.setdefault((source, book, market), Row())
        row.games += 1
        row.open += f["open"]
        row.current += f["current"]
        if tip[gid] <= now:
            row.started += 1
            row.close += f["close"]
            if (gap := _tip_gap(runs[source], tip[gid])) is not None:
                row.tip_gaps.append(gap)
    for market in MARKETS:
        closed = {k[0] for k, f in seen.items() if k[3] == market and f["close"]}
        current = {k[0] for k, f in seen.items() if k[3] == market and f["current"]}
        cov.no_close[market] = [label[g] for g in cov.started if g not in closed]
        cov.no_current[market] = [label[g] for g in cov.upcoming if g not in current]

    pseen: dict[tuple, dict] = defaultdict(lambda: dict(open=False, close=False, polls=0))
    q = (select(prop_lines.c.game_id, prop_lines.c.source, books.c.key.label("book"),
                prop_lines.c.kind, prop_lines.c.quoted_at, prop_lines.c.fetched_at)
         .join(books, books.c.book_id == prop_lines.c.book_id))  # fmt: skip
    for r in _rows(conn, q, prop_lines.c.game_id, ids):
        f, t = pseen[(r.game_id, r.source, r.book)], tip[r.game_id]
        f["open"] |= r.kind in ("open", "poll")
        f["close"] |= (r.kind == "close" and r.quoted_at is not None and r.quoted_at < t) or (
            r.kind == "poll" and r.fetched_at <= t)
        f["polls"] += r.kind == "poll"
    for (gid, source, book), f in pseen.items():
        row = cov.props.setdefault((source, book), Row())
        row.games += 1
        row.open += f["open"]
        row.polls += f["polls"]
        if tip[gid] <= now:
            row.started += 1
            row.close += f["close"]
    return cov


def report(cov: Coverage) -> list[str]:
    out = [f"{cov.sport} lines, games from {cov.since:%Y-%m-%d %H:%M} to "
           f"{cov.until:%Y-%m-%d %H:%M} UTC: {len(cov.started)} started, "
           f"{len(cov.upcoming)} upcoming",
           f"{'source':14} {'book':12} {'market':8} {'games':>5} {'open':>5} {'current':>7} "
           f"{'close':>9} {'last poll':>9}"]  # fmt: skip
    for (source, book, market), r in sorted(cov.lines.items()):
        gap = f"{median(r.tip_gaps):.0f} min" if r.tip_gaps else "-"
        out.append(f"{source:14} {book:12} {market:8} {r.games:5} {r.open:5} {r.current:7} "
                   f"{f'{r.close}/{r.started}':>9} {gap:>9}")  # fmt: skip
    if not cov.lines:
        out.append("(no game lines stored for these games)")
    out.append(f"{'props source':14} {'book':12} {'':8} {'games':>5} {'open':>5} {'polls':>7} "
               f"{'close':>9}")  # fmt: skip
    for (source, book), r in sorted(cov.props.items()):
        out.append(f"{source:14} {book:12} {'':8} {r.games:5} {r.open:5} {r.polls:7} "
                   f"{f'{r.close}/{r.started}':>9}")  # fmt: skip
    if not cov.props:
        out.append("(no prop lines stored for these games)")
    for market in MARKETS:
        miss, ahead = cov.no_close[market], cov.no_current[market]
        out.append(f"{market}: started without a pre-game close {len(miss)}/{len(cov.started)}"
                   f"; upcoming without a current line {len(ahead)}/{len(cov.upcoming)}")
        out += [f"  no close: {g}" for g in miss[:10]] + [f"  no line: {g}" for g in ahead[:10]]
    return out
