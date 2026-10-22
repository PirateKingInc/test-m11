"""ESPN player prop lines (free, no key; unofficial API, ≤1 request/s), NBA and NFL (M15).

  lines     upcoming games (--sport nba|nfl): each book's current main line, kind `poll`
  history   finished games of a season (--sport --season): opening and closing lines, kinds
            `open` and `close`, from at most two books per game (HISTORY_ORDER)

Per game: the providers list (.../competitions/ID/odds), then each provider's propBets (paged;
404 = no props from that book). Items are reduced to the kept markets before the run's one
archived body. NFL games have no ESPN id in the schedule: ESPN's scoreboard is matched to our
games by date and teams, recorded as `espn_nfl` in game_source_ids.

Main line parse rules (MODEL.md "M15 data sources"): labelled items (`over`/`under` in
`current` or `open`) are the main line's sides; otherwise exactly two unlabelled items on one
target are a pair, the first being the over (DraftKings, Dec 2025 on); an unpriced pair is
kept with null prices; one-sided items and ladders are alternates and skipped. Anytime TD: the
yes price, kept only when priced. Rows are stored change-only per (game, player, market, book,
kind).

NFL games more than 2 days away are refreshed only on the 7, 11, 15 and 19 ET runs.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, func, insert, select

from betdata.config import SourceConfig
from betdata.core import raw_store
from betdata.core.errors import ApiError, ConfigError, NothingToDo
from betdata.core.http import SourceClient
from betdata.core.matching import _candidates, sport_id
from betdata.core.normalize.catalog import BookResolver, book_key
from betdata.core.normalize.teams import TeamResolver
from betdata.core.source import JobSpec, RawInput, Source
from betdata.db.schema import game_source_ids, games, nba_player_games, prop_lines
from betdata.sources.espn_nba import book_of

NAME = "espn_props"
log = logging.getLogger(__name__)
EASTERN = ZoneInfo("America/New_York")
CORE = "https://sports.core.api.espn.com/v2/sports"
LEAGUE = {"nba": "basketball/leagues/nba", "nfl": "football/leagues/nfl"}
NFL_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ID_SOURCES = {"nba": ("espn", "espn_nba"), "nfl": ("espn_nfl",)}
AHEAD = {"nba": timedelta(days=2), "nfl": timedelta(days=8)}
NEAR = timedelta(days=2)  # NFL games beyond this are refreshed every 4 hours only
HISTORY_ORDER = ("58", "100", "40", "47", "45")  # ESPN BET, DraftKings, DK (old), MGM, Caesars
MARKETS = {
    "Total Points": "points", "Total Rebounds": "rebounds", "Total Assists": "assists",
    "Total 3-Point Field Goals": "threes", "Total Points, Rebounds, and Assists": "pra",
    "Total Passing Yards (incl. overtime)": "pass_yds",
    "Total Rushing Yards (incl. overtime)": "rush_yds",
    "Total Receiving Yards (incl. overtime)": "rec_yds",
    "Total Receptions (incl. overtime)": "receptions",
    "Anytime Touchdown Scorer": "anytime_td",
}  # fmt: skip
Q = {"lang": "en", "region": "us"}


# --- parse (pure) ------------------------------------------------------------------------


def american(v) -> int | None:
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() in ("even", "ev"):
        return 100
    try:
        x = float(s.replace("+", ""))
    except ValueError:
        return None
    return int(round(x)) if abs(x) >= 100 else None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _side(it: dict) -> str | None:
    for part in ("current", "open"):
        for s in ("over", "under"):
            if s in (it.get(part) or {}):
                return s
    return None


def _target(it: dict, part: str) -> float | None:
    v = ((it.get(part) or {}).get("target") or {}).get("value")
    if v is None:
        total = (it.get("odds") or {}).get("total") or {}
        v = total.get("value" if part == "current" else "open")
    return _num(v)


def _price(it: dict | None, part: str, side: str = "over") -> int | None:
    if it is None:
        return None
    p = american(((it.get(part) or {}).get(side) or {}).get("american"))
    if p is None:
        p = american(((it.get("odds") or {}).get("american") or {}).get(
            "value" if part == "current" else "open"))  # fmt: skip
    return p


def _implied(p: int) -> float:
    return 100 / (p + 100) if p > 0 else -p / (-p + 100)


def main_line(market: str, items: list[dict]) -> dict | None:
    """One (player, market)'s main line: {"close": (line, over, under), "open": (...),
    "quoted_at"}, or None when only alternates (or nothing priced, for anytime TD) exist."""
    if market == "anytime_td":
        it = next((i for i in items if _price(i, "current") is not None), None)
        if it is None:
            return None
        return {"close": (None, _price(it, "current"), None),
                "open": (None, _price(it, "open"), None), "quoted_at": it.get("lastUpdated")}
    labelled = [i for i in items if _side(i)]
    if labelled:
        by: dict = defaultdict(dict)
        for i in labelled:
            by[_target(i, "current")].setdefault(_side(i), i)
        both = [t for t, s in by.items() if len(s) == 2]
        if both:  # several labelled pairs (ladders labelled too): the most balanced is the main

            def gap(t):
                o = (_price(by[t]["over"], "current", "over")
                     or _price(by[t]["over"], "open", "over"))  # fmt: skip
                u = (_price(by[t]["under"], "current", "under")
                     or _price(by[t]["under"], "open", "under"))  # fmt: skip
                return abs(_implied(o) - _implied(u)) if o is not None and u is not None else 9.0

            t = min(both, key=gap)
        else:
            t = next(iter(by))
        o, u = by[t].get("over"), by[t].get("under")
        ref = o or u
        return {"close": (t, _price(o, "current", "over"), _price(u, "current", "under")),
                "open": (_target(ref, "open"), _price(o, "open", "over"),
                         _price(u, "open", "under")),
                "quoted_at": ref.get("lastUpdated")}  # fmt: skip
    by = defaultdict(list)
    for i in items:
        by[_target(i, "current")].append(i)
    pairs = [v for t, v in by.items() if t is not None and len(v) == 2]
    priced = [v for v in pairs if all(_price(i, "current") is not None for i in v)]
    if priced:  # several pairs are not expected; take the most balanced one
        o, u = min(priced, key=lambda v: abs(_implied(_price(v[0], "current"))
                                             - _implied(_price(v[1], "current"))))  # fmt: skip
    else:
        unpriced = [v for v in pairs if all(_price(i, "current") is None for i in v)]
        if not unpriced:
            return None
        o, u = unpriced[0]
    return {"close": (_target(o, "current"), _price(o, "current"), _price(u, "current")),
            "open": (_target(o, "open"), _price(o, "open"), _price(u, "open")),
            "quoted_at": o.get("lastUpdated")}  # fmt: skip


def athlete_id(item: dict) -> str | None:
    m = re.search(r"/athletes/(\d+)", str((item.get("athlete") or {}).get("$ref", "")))
    return m.group(1) if m else None


def keep_items(items: list[dict]) -> list[dict]:
    """Player items of kept markets, reduced to the fields the parse reads (archive size)."""
    out = []
    for it in items:
        pid, market = athlete_id(it), MARKETS.get(str((it.get("type") or {}).get("name")))
        if pid and market:
            out.append({"athlete": pid, "market": market, "lastUpdated": it.get("lastUpdated"),
                        **{k: it[k] for k in ("odds", "current", "open") if k in it}})
    return out


def _quoted(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_payload(body: bytes) -> list[dict]:
    """Rows to store from an archived body ({kind, games: [{espn_game_id, providers: [{id,
    name, items}]}]}): one per game, player, market, book and kind."""
    data = json.loads(body)
    kinds = ("open", "close") if data["kind"] == "history" else ("poll",)
    rows = []
    for g in data["games"]:
        for p in g["providers"]:
            book = book_of(p["name"]) or book_key(p["name"])
            groups: dict = defaultdict(list)
            for it in p["items"]:
                groups[(it["athlete"], it["market"])].append(it)
            for (pid, market), items in groups.items():
                main = main_line(market, items)
                if main is None:
                    continue
                for kind in kinds:
                    line, over, under = main["open" if kind == "open" else "close"]
                    if line is None and over is None and under is None:
                        continue
                    rows.append(dict(espn_game_id=g["espn_game_id"], player_id=pid,
                                     market=market, book=book, book_name=p["name"], kind=kind,
                                     line=line, over_price=over, under_price=under,
                                     quoted_at=_quoted(main["quoted_at"])))  # fmt: skip
    return rows


# --- games ---------------------------------------------------------------------------------


def espn_ids(conn: Connection, sport: str, *where) -> dict[str, int]:
    """ESPN event id -> game_id for this sport's games matching `where`."""
    q = (select(game_source_ids.c.source_game_id, game_source_ids.c.game_id)
         .join(games, games.c.game_id == game_source_ids.c.game_id)
         .where(game_source_ids.c.source.in_(ID_SOURCES[sport]),
                games.c.sport_id == sport_id(conn, sport), *where)
         .order_by(games.c.start_time_utc))  # fmt: skip
    return dict(conn.execute(q).all())


def match_events(conn: Connection, sport: str, events: list[dict]) -> dict[str, tuple[int, bool]]:
    """ESPN events ({id, date, home, away}: lists of team names or abbreviations) -> (our
    game_id, home/away swapped), by teams within the matching window. Never creates games."""
    sid = sport_id(conn, sport)
    teams = TeamResolver(conn, sid)
    out = {}
    for e in events:
        h, a = (next((t for t in (teams.lookup(f"espn_{sport}", n) for n in e[k]) if t), None)
                for k in ("home", "away"))  # fmt: skip
        if h is None or a is None:
            continue
        start = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
        found = _candidates(conn, sid, h, a, start)  # closest kickoff, either orientation
        if found:
            out[e["id"]] = found[0]
    return out


def record_nfl_ids(conn: Connection, events: list[dict]) -> None:
    """Record matched ESPN NFL event ids as source `espn_nfl` (nflverse rows untouched)."""
    if not events:
        return
    have = set(conn.execute(select(game_source_ids.c.source_game_id).where(
        game_source_ids.c.source == "espn_nfl")).scalars())
    new = {e: v for e, v in match_events(conn, "nfl", events).items() if e not in have}
    if new:
        conn.execute(insert(game_source_ids), [
            dict(source="espn_nfl", source_game_id=e, game_id=g, home_away_swapped=s)
            for e, (g, s) in new.items()])  # fmt: skip


def _player_names(conn: Connection, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    last = (select(nba_player_games.c.player_id, func.max(nba_player_games.c.player_name))
            .where(nba_player_games.c.player_id.in_(sorted(ids)))
            .group_by(nba_player_games.c.player_id))  # fmt: skip
    return dict(conn.execute(last).all())


def store_rows(conn: Connection, sport: str, rows: list[dict], *, fetched_at, raw_id) -> int:
    """Change-only: a row is inserted when its key's (line, prices) differ from the key's last
    stored row."""
    gids = {r["espn_game_id"] for r in rows}
    if not gids:
        return 0
    game_of = espn_ids(conn, sport, game_source_ids.c.source_game_id.in_(sorted(gids)))
    books = BookResolver(conn, NAME)
    last: dict = {}
    for r in conn.execute(select(prop_lines).where(prop_lines.c.source == NAME,
                                                   prop_lines.c.source_game_id.in_(sorted(gids)))
                          .order_by(prop_lines.c.fetched_at)).mappings():  # fmt: skip
        key = (r["source_game_id"], r["player_id"], r["market"], r["book_id"], r["kind"])
        last[key] = (r["line"], r["over_price"], r["under_price"])
    names = _player_names(conn, {r["player_id"] for r in rows}) if sport == "nba" else {}
    out, seen = [], set()
    for r in rows:
        bid = books.resolve(r["book"], r["book_name"])
        key = (r["espn_game_id"], r["player_id"], r["market"], bid, r["kind"])
        val = (r["line"], r["over_price"], r["under_price"])
        if key in seen or last.get(key, ...) == val:
            continue  # unchanged, or a second provider for the same book
        seen.add(key)
        out.append(dict(source=NAME, source_game_id=r["espn_game_id"],
                        game_id=game_of.get(r["espn_game_id"]), player_id=r["player_id"],
                        player_name=r.get("player_name") or names.get(r["player_id"]),
                        market=r["market"], book_id=bid, kind=r["kind"], line=r["line"],
                        over_price=r["over_price"], under_price=r["under_price"],
                        quoted_at=r["quoted_at"], fetched_at=r.get("fetched_at") or fetched_at,
                        raw_id=raw_id))  # fmt: skip
    for i in range(0, len(out), 1000):
        conn.execute(insert(prop_lines), out[i:i + 1000])
    return len(out)


# --- source --------------------------------------------------------------------------------


class EspnPropsSource(Source):
    name = NAME

    def __init__(self, engine, config: SourceConfig, *, transport=None, sleep=None, clock=None):
        super().__init__(engine, config, clock)
        extra = {k: v for k, v in dict(sleep=sleep, clock=clock).items() if v is not None}
        # One client for both hosts, so the 1 request/s limit holds across them. 404s (no
        # props) are normal and must not open the breaker.
        self.client = SourceClient(engine, NAME, config.options.get("base_url", CORE),
                                   transport=transport, max_attempts=3,
                                   min_interval=config.options.get("min_interval_seconds", 1.0),
                                   breaker_threshold=10_000, **extra)  # fmt: skip

    @classmethod
    def jobs(cls) -> dict[str, JobSpec]:
        return {"lines": JobSpec("lines", "Current prop lines of upcoming games (--sport nba|nfl)"),
                "history": JobSpec("history", "Opening and closing prop lines of a season's "
                                   "finished games (--sport nba --season 2024)")}  # fmt: skip

    def _json(self, url: str, params: dict, run_id: int) -> dict | None:
        """GET -> JSON; None on 404. An empty or broken body is retried once after a pause."""
        for attempt in (1, 2):
            try:
                body = self.client.get(url, params, est_cost=0, run_id=run_id,
                                       store_raw=False).body  # fmt: skip
            except ApiError as e:
                if e.status == 404:
                    return None
                raise
            try:
                data = json.loads(body)
                if data:
                    return data
            except ValueError:
                pass
            if attempt == 1:
                self.client.sleep(5.0)
        raise ApiError(f"{NAME} {url}: empty response")

    def _props(self, sport: str, eid: str, run_id: int, providers: int | None) -> list[dict]:
        base = f"/{LEAGUE[sport]}/events/{eid}/competitions/{eid}/odds"
        listed = (self._json(base, Q, run_id) or {}).get("items", [])
        provs = [(str(p["provider"]["id"]), str(p["provider"].get("name", "")))
                 for p in listed if p.get("provider")]  # fmt: skip
        provs = [(i, n) for i, n in dict(provs).items()
                 if i.isdigit() and int(i) <= 999 and "live" not in n.lower()]  # fmt: skip
        if providers is not None:  # history: preferred books first
            rank = {p: k for k, p in enumerate(HISTORY_ORDER)}
            provs.sort(key=lambda p: rank.get(p[0], len(rank)))
        out = []
        for pid, pname in provs:
            items, page, pages = [], 1, 1
            while page <= pages:
                params = {**Q, "limit": 1000, **({"page": page} if page > 1 else {})}
                data = self._json(f"{base}/{pid}/propBets", params, run_id)
                if data is None:
                    break
                items += data.get("items", [])
                pages, page = int(data.get("pageCount") or 1), page + 1
            kept = keep_items(items)
            if kept:
                out.append({"id": pid, "name": pname, "items": kept})
            if providers is not None and len(out) >= providers:
                break
        return out

    def _map_nfl(self, where, run_id: int) -> list[dict]:
        """Scoreboard events on the Eastern dates of NFL games (matching `where`) that have no
        ESPN id yet, reduced to {id, date, home, away}."""
        with self.engine.connect() as conn:
            mapped = select(game_source_ids.c.game_id).where(
                game_source_ids.c.source == "espn_nfl")
            starts = conn.execute(select(games.c.start_time_utc).where(
                games.c.sport_id == sport_id(conn, "nfl"), games.c.game_id.not_in(mapped),
                *where)).scalars()  # fmt: skip
            days = sorted({t.astimezone(EASTERN).date() for t in starts})
        events = []
        for day in days:
            data = self._json(NFL_SCOREBOARD, {"dates": f"{day:%Y%m%d}"}, run_id) or {}
            for e in data.get("events", []):
                side = {c["homeAway"]: c["team"] for c in e["competitions"][0]["competitors"]}
                events.append({"id": str(e["id"]), "date": e["date"], **{
                    k: [side[k].get("displayName", ""), side[k].get("abbreviation", "")]
                    for k in ("home", "away")}})  # fmt: skip
        return events

    def fetch(self, job: str, params: dict[str, Any], *, priority: str, run_id: int) -> RawInput:
        sport = str(params.get("sport", ""))
        if sport not in LEAGUE:
            raise ConfigError(f"{NAME}: --sport must be nba or nfl, not {sport!r}")
        now = self.clock()
        if job == "lines":
            where = (games.c.status == "scheduled", games.c.start_time_utc > now,
                     games.c.start_time_utc <= now + AHEAD[sport])  # fmt: skip
            providers = None
        elif job == "history":
            season = int(params["season"])
            where = (games.c.season == season, games.c.status == "final",
                     games.c.start_time_utc < now - timedelta(hours=6))  # fmt: skip
            providers = 2
        else:
            raise KeyError(job)
        events = self._map_nfl(where, run_id) if sport == "nfl" else []
        with self.engine.connect() as conn:
            starts = dict(conn.execute(select(games.c.game_id, games.c.start_time_utc).where(
                games.c.sport_id == sport_id(conn, sport), *where)).all())
            known = {e: g for e, g in espn_ids(conn, sport, *where).items()}
            known |= {e: g for e, (g, _) in match_events(conn, "nfl", events).items()
                      if g in starts}  # a scoreboard day also lists other games
            if job == "history":  # resumable: games with stored closing lines are done
                done = set(conn.execute(select(prop_lines.c.source_game_id).where(
                    prop_lines.c.source == NAME, prop_lines.c.kind == "close")).scalars())
                known = {e: g for e, g in known.items() if e not in done}
        hour_ok = now.astimezone(EASTERN).hour % 4 == 3  # 7, 11, 15, 19 ET
        ids = [e for e, g in sorted(known.items(), key=lambda kv: starts[kv[1]])
               if job == "history" or hour_ok or starts[g] <= now + NEAR]
        ids = ids[:int(params.get("limit", 5000))]
        if not ids:
            raise NothingToDo(f"no {sport} games with an ESPN id to fetch")
        out = []
        for eid in ids:
            try:  # one game's failure must not stop the others
                out.append({"espn_game_id": eid,
                            "providers": self._props(sport, eid, run_id, providers)})
            except ApiError as e:
                log.warning("%s %s game %s: %s", NAME, job, eid, e)
        body = json.dumps({"sport": sport, "kind": "poll" if job == "lines" else job,
                           "events": events, "games": out}).encode()  # fmt: skip
        at = self.clock()
        with self.engine.begin() as conn:
            raw_id = raw_store.save(conn, source=NAME, endpoint=f"/{job}", params=params,
                                    http_status=200, fetched_at=at, body=body,
                                    content_type="application/json")  # fmt: skip
        return RawInput(f"/{job}", params, body, at, raw_id)

    def job_for_endpoint(self, endpoint: str) -> str:
        return endpoint.strip("/")

    def parse(self, job: str, raw: RawInput):
        return json.loads(raw.body), parse_payload(raw.body), raw.fetched_at

    def load(self, conn, job: str, batch, *, run_id, raw_id) -> tuple[int, int]:
        payload, rows, fetched_at = batch
        if payload["sport"] == "nfl":
            record_nfl_ids(conn, payload["events"])
        n = store_rows(conn, payload["sport"], rows, fetched_at=fetched_at, raw_id=raw_id)
        return n, len(rows)
