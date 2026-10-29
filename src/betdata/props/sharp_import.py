"""DK/FD player prop lines from our own archive into `prop_lines` (M18 Part E).

Since M18 no ESPN prop endpoint is called. The poller's opener sweeps and polls store SharpAPI's
DK/FD props in the poller database (`quotes`, one row per side); each hourly run copies what is
new into `prop_lines` (source "sharpapi", kind "poll"), so the prop models, the page and prop
grading read them as they read ESPN's lines before.

- **Pairing:** each (game, book, market, player) is replayed in poll order; a row is written at
  each poll where both the over and the under are known at the same line, change-only against
  the key's last stored row.
- **Player ids:** NBA lines carry the ESPN athlete id of the player with that normalised name in
  `nba_player_games` (unique names only); NFL lines and unmatched NBA names carry
  "name:<normalised name>" (the NFL id map falls back to the name, `nfl/props/ids.py`).
- **Markets:** SharpAPI's (`alerts/feeds.PROPS`), stored under the prop models' market keys.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import Connection, func, insert, select

from betdata.alerts.db import quotes
from betdata.core.normalize.catalog import BookResolver
from betdata.db.schema import nba_player_games, prop_lines
from betdata.nfl.props.ids import norm_name

NAME = "sharpapi"
# poller prop market -> prop_lines market (the models' keys; NFL as nflp15 / ESPN stored them)
MARKETS = {"points": "points", "rebounds": "rebounds", "assists": "assists",
           "threes": "threes", "rec_yds": "rec_yds", "rush_yds": "rush_yds",
           "receptions": "receptions"}  # fmt: skip


def nba_ids(conn: Connection) -> dict[str, str]:
    """Normalised name -> ESPN athlete id, for names held by exactly one id."""
    rows = conn.execute(select(nba_player_games.c.player_name, nba_player_games.c.player_id)
                        .distinct()).all()  # fmt: skip
    by: dict[str, set[str]] = defaultdict(set)
    for name, pid in rows:
        by[norm_name(name)].add(str(pid))
    return {k: next(iter(v)) for k, v in by.items() if len(v) == 1}


def import_props(main: Connection, poller: Connection | None, now: datetime) -> int:
    """Copy new SharpAPI prop quotes polled up to `now` into `prop_lines`; returns rows added."""
    if poller is None:
        return 0
    since = main.execute(select(func.max(prop_lines.c.fetched_at))
                         .where(prop_lines.c.source == NAME)).scalar()  # fmt: skip
    gids = sorted(set(poller.execute(
        select(quotes.c.game_id).distinct()
        .where(quotes.c.source == NAME, quotes.c.player_name.is_not(None),
               quotes.c.polled_at <= now,
               *([quotes.c.polled_at > since] if since is not None else []))).scalars()))
    if not gids:
        return 0
    rows = poller.execute(select(quotes).where(
        quotes.c.source == NAME, quotes.c.player_name.is_not(None),
        quotes.c.game_id.in_(gids), quotes.c.polled_at <= now,
        quotes.c.market.in_(list(MARKETS))).order_by(quotes.c.polled_at, quotes.c.quote_id)).all()
    sport_of = {r.game_id: r.sport for r in rows}
    ids = nba_ids(main) if "nba" in sport_of.values() else {}
    books = BookResolver(main, NAME)
    last: dict[tuple, tuple] = {}
    for r in main.execute(select(prop_lines).where(prop_lines.c.source == NAME,
                                                   prop_lines.c.game_id.in_(gids))
                          .order_by(prop_lines.c.fetched_at)).mappings():  # fmt: skip
        last[(r["game_id"], r["book_id"], r["market"], r["player_id"])] = (
            r["line"], r["over_price"], r["under_price"])
    state: dict[tuple, dict] = {}
    out: list[dict] = []
    for r in rows:
        k = norm_name(r.player_name)
        pid = (ids.get(k) if sport_of[r.game_id] == "nba" else None) or f"name:{k}"
        key = (r.game_id, books.resolve(r.book), MARKETS[r.market], pid)
        s = state.setdefault(key, {})
        s[r.outcome] = (r.line, r.price)
        o, u = s.get("over"), s.get("under")
        if o is None or u is None or o[0] != u[0]:
            continue
        val = (o[0], o[1], u[1])
        if last.get(key) == val:
            continue
        polled = r.polled_at
        if since is not None and polled <= since:
            last[key] = val  # already imported up to here; carry the state only
            continue
        last[key] = val
        out.append(dict(source=NAME, source_game_id=str(r.game_id), game_id=r.game_id,
                        player_id=pid, player_name=r.player_name, market=key[2],
                        book_id=key[1], kind="poll", line=val[0], over_price=val[1],
                        under_price=val[2], quoted_at=r.book_updated_at, fetched_at=polled,
                        raw_id=None))  # fmt: skip
    # one row per key and poll time (the unique key): keep the last
    dedup = {(d["game_id"], d["book_id"], d["market"], d["player_id"], d["fetched_at"]): d
             for d in out}
    out = list(dedup.values())
    for i in range(0, len(out), 1000):
        main.execute(insert(prop_lines), out[i:i + 1000])
    return len(out)
