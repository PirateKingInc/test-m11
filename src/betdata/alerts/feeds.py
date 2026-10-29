"""The poller's price feeds (M16 Part B, measured live 2026-10-22, PROJECT.md "M16 sources"):

- **SharpAPI** (free: DraftKings + FanDuel, 12 requests a minute, 60 s delay, 200 rows a call
  with no paging metadata): main lines and player props for one game or a whole league.
- **Pinnacle** through The Odds API (`bookmakers=pinnacle`, 1 credit per market; the 500
  monthly credits are shared with the hourly job, so a reserve for it is always kept).
- **Kalshi** (public market data, no key): game winner markets, a secondary probability.

Every request is spaced (SharpAPI 6 s, i.e. <= 10 a minute; others 1 s) and counted. Events are
matched to the main database's games read-only, by stored source id or by teams and kickoff.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import Connection, select

from betdata.core.matching import sport_id
from betdata.core.normalize.teams import TeamResolver
from betdata.db.schema import game_source_ids, games, teams

log = logging.getLogger(__name__)

SHARP_BASE = "https://api.sharpapi.io/api/v1"
ODDS_BASE = "https://api.the-odds-api.com/v4"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SHARP_PAGE = 200  # the free tier's rows per call
SHARP_MAX_PAGES = 6
MAIN = {"moneyline": "h2h", "point_spread": "spreads", "total_points": "totals"}
# SharpAPI prop market -> our market (MODEL.md Signal 1 prop markets), names checked live
# 2026-10-22.
PROPS = {
    "nba": {"player_points": "points", "player_rebounds": "rebounds",
            "player_assists": "assists", "player_made_threes": "threes"},
    "nfl": {"player_receiving_yards": "rec_yds", "player_rushing_yards": "rush_yds",
            "player_receptions": "receptions"},
}  # fmt: skip
ODDS_KEYS = {"nba": "basketball_nba", "nfl": "americanfootball_nfl"}
KALSHI_SERIES = {"nba": "KXNBAGAME", "nfl": "KXNFLGAME"}
KALSHI_ALIAS = {"nfl": {"LAR": "LA", "JAC": "JAX", "WSH": "WAS", "LVR": "LV"}}
# The Odds API: the hourly job's own need (PROJECT.md quota table, ~159 credits a 30-day month)
# is reserved for the rest of the month, plus a fixed safety margin.
HOURLY_CREDITS_PER_DAY = 159 / 30
ODDS_SAFETY = 50


@dataclass
class Quote:
    sport: str
    game_id: int
    source: str
    book: str
    market: str
    outcome: str
    line: float | None
    price: int | None
    player_name: str | None = None
    book_updated_at: datetime | None = None


class Http:
    """One client for the poller: per-host spacing and a request count per source."""

    SPACING = {"sharpapi": 6.0, "the_odds_api": 1.0, "kalshi": 1.0, "espn": 1.0,
               "nba_injury": 1.0}  # fmt: skip

    def __init__(self, transport: httpx.BaseTransport | None = None, sleep=time.sleep,
                 monotonic=time.monotonic):  # fmt: skip
        self.client = httpx.Client(timeout=30, transport=transport, follow_redirects=True,
                                   headers={"User-Agent": "betdata-m16-poller"})  # fmt: skip
        self.sleep, self.monotonic = sleep, monotonic
        self.last: dict[str, float] = {}
        self.count: Counter = Counter()

    def get(self, source: str, url: str, **kw) -> httpx.Response:
        gap = self.SPACING.get(source, 1.0) - (self.monotonic() - self.last.get(source, -1e9))
        if gap > 0:
            self.sleep(gap)
        self.last[source] = self.monotonic()
        self.count[source] += 1
        return self.client.get(url, **kw)


# --- game matching (read-only on the main database) --------------------------------------------


def match_game(conn: Connection, sport: str, source: str, event_id: str | None, home: str,
               away: str, start: datetime | None) -> int | None:  # fmt: skip
    """The main database's game for a source event: its stored id, else the same two teams
    with the closest kickoff within 36 hours."""
    if event_id:
        gid = conn.execute(select(game_source_ids.c.game_id).where(
            game_source_ids.c.source == source,
            game_source_ids.c.source_game_id == event_id)).scalar()  # fmt: skip
        if gid is not None:
            return gid
    if start is None:
        return None
    res = TeamResolver(conn, sport_id(conn, sport))
    h, a = res.lookup(source, home), res.lookup(source, away)
    if h is None or a is None:
        return None
    return _closest(conn, sport, h, a, start)


def _closest(conn, sport, h, a, start) -> int | None:
    rows = conn.execute(select(games.c.game_id, games.c.start_time_utc).where(
        games.c.sport_id == sport_id(conn, sport), games.c.home_team_id == h,
        games.c.away_team_id == a,
        games.c.start_time_utc.between(start - timedelta(hours=36),
                                       start + timedelta(hours=36)))).all()  # fmt: skip
    if not rows:
        return None
    return min(rows, key=lambda r: abs(_aware(r.start_time_utc) - _aware(start))).game_id


def _aware(t: datetime) -> datetime:
    from datetime import UTC

    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _when(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None


# --- SharpAPI ------------------------------------------------------------------------------------


class SharpFeed:
    def __init__(self, http: Http, api_key: str):
        self.http, self.key = http, api_key

    def _rows(self, params: dict, horizon: datetime | None = None,
              main: bool = True) -> list[dict]:  # fmt: skip
        out: list[dict] = []
        for page in range(SHARP_MAX_PAGES):
            r = self.http.get("sharpapi", f"{SHARP_BASE}/odds", headers={"X-API-Key": self.key},
                              params={**params, "is_main_line": "true" if main else "false",
                                      "live": "false",
                                      "sportsbook": "draftkings,fanduel", "limit": 500,
                                      "offset": page * SHARP_PAGE})  # fmt: skip
            r.raise_for_status()
            data = r.json().get("data") or []
            out += data
            if len(data) < SHARP_PAGE:
                break
            starts = [_when(d.get("event_start_time")) for d in data if d.get("event_start_time")]
            if horizon is not None and starts and max(starts) > horizon:
                break
        if not main:
            return [d for d in out if d.get("is_main_line") is False]
        return [d for d in out if d.get("is_main_line") is not False]

    def board(self, conn: Connection, sport: str, horizon: datetime) -> list[Quote]:
        """Main full-game lines of every game up to `horizon` (one to a few calls)."""
        rows = self._rows({"league": sport, "market": ",".join(MAIN)}, horizon)
        return self._quotes(conn, sport, rows)

    def alternates(self, conn: Connection, sport: str, horizon: datetime) -> list[Quote]:
        """M18 G2: alternate full-game spread and total rungs (is_main_line=false), stored under
        the markets "alt_spreads" / "alt_totals" so no main-line reader ever sees them."""
        rows = self._rows({"league": sport, "market": "point_spread,total_points"}, horizon,
                          main=False)  # fmt: skip
        return [Quote(q.sport, q.game_id, q.source, q.book, "alt_" + q.market, q.outcome,
                      q.line, q.price, None, q.book_updated_at)
                for q in self._quotes(conn, sport, rows) if q.market in ("spreads", "totals")]

    def game(self, conn: Connection, sport: str, event_id: str) -> list[Quote]:
        """One game's main lines and the Signal 1 prop markets (a few calls)."""
        markets = ",".join([*MAIN, *PROPS[sport]])
        rows = self._rows({"league": sport, "event_id": event_id, "market": markets})
        return self._quotes(conn, sport, rows)

    def _quotes(self, conn, sport, rows) -> list[Quote]:
        gid_of: dict[str, int | None] = {}
        out = []
        for d in rows:
            ev = d.get("event_id")
            if ev not in gid_of:
                gid_of[ev] = match_game(conn, sport, "sharpapi", ev, d.get("home_team", ""),
                                        d.get("away_team", ""), _when(d.get("event_start_time")))
            gid = gid_of[ev]
            mt = (d.get("market_type") or "").lower()
            if gid is None or (d.get("market_segment") or "") not in ("", "full_game", "game"):
                continue
            player = d.get("player_name")
            market = PROPS[sport].get(mt) if player else MAIN.get(mt)
            if market is None or d.get("odds_american") is None:
                continue
            outcome = _sharp_outcome(d, market)
            if outcome is None:
                continue
            out.append(Quote(sport, gid, "sharpapi", d["sportsbook"], market, outcome,
                             d.get("line") if market != "h2h" else None,
                             round(d["odds_american"]), player,
                             _when(d.get("timestamp"))))  # fmt: skip
        return out

    def event_ids(self, conn: Connection, game_id: int) -> list[str]:
        return list(conn.execute(select(game_source_ids.c.source_game_id).where(
            game_source_ids.c.source == "sharpapi",
            game_source_ids.c.game_id == game_id)).scalars())  # fmt: skip


def _sharp_outcome(d: dict, market: str) -> str | None:
    if market in ("h2h", "spreads"):
        side = (d.get("team_side") or d.get("selection_type") or "").lower()
        if side in ("home", "away"):
            return side
        if d.get("selection") == d.get("home_team"):
            return "home"
        return "away" if d.get("selection") == d.get("away_team") else None
    text = (d.get("selection_type") or d.get("selection") or "").lower()
    return "over" if text.startswith("over") else "under" if text.startswith("under") else None


# --- Pinnacle through The Odds API --------------------------------------------------------------


class PinnacleFeed:
    def __init__(self, http: Http, api_key: str):
        self.http, self.key = http, api_key
        self.remaining: int | None = None

    def _note(self, r: httpx.Response) -> None:
        rem = r.headers.get("x-requests-remaining")
        if rem is not None and rem.replace(".", "").isdigit():
            self.remaining = int(float(rem))

    def reserve(self, now: datetime) -> int:
        """Credits kept for the hourly job until the month resets, plus a safety margin."""
        first_next = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
        days_left = max(0.0, (first_next - now).total_seconds() / 86400)
        return int(HOURLY_CREDITS_PER_DAY * days_left + ODDS_SAFETY + 0.999)

    def affordable(self, now: datetime, cost: int) -> bool:
        if self.remaining is None:  # the free /sports call reports the balance at no cost
            r = self.http.get("the_odds_api", f"{ODDS_BASE}/sports", params={"apiKey": self.key})
            self._note(r)
        return self.remaining is not None and self.remaining - cost >= self.reserve(now)

    def board(self, conn: Connection, sport: str, now: datetime,
              markets: tuple[str, ...] = ("h2h", "spreads", "totals"),
              ) -> list[Quote] | None:  # fmt: skip
        """Pinnacle's main lines for the sport (cost: one credit per market), or None when the
        hourly job's reserve would be touched."""
        if not self.affordable(now, len(markets)):
            log.info("pinnacle board skipped: %s credits left, reserve %s", self.remaining,
                     self.reserve(now))
            return None
        r = self.http.get("the_odds_api", f"{ODDS_BASE}/sports/{ODDS_KEYS[sport]}/odds",
                          params={"apiKey": self.key, "bookmakers": "pinnacle",
                                  "markets": ",".join(markets), "oddsFormat": "american"})
        self._note(r)
        r.raise_for_status()
        out = []
        for ev in r.json():
            gid = match_game(conn, sport, "the_odds_api", ev.get("id"), ev.get("home_team", ""),
                             ev.get("away_team", ""), _when(ev.get("commence_time")))
            if gid is None:
                continue
            for bm in ev.get("bookmakers") or []:
                for mk in bm.get("markets") or []:
                    for oc in mk.get("outcomes") or []:
                        side = _odds_outcome(mk["key"], oc.get("name"), ev)
                        if side is None:
                            continue
                        out.append(Quote(sport, gid, "the_odds_api", bm["key"], mk["key"], side,
                                         oc.get("point") if mk["key"] != "h2h" else None,
                                         round(oc["price"]), None,
                                         _when(mk.get("last_update"))))  # fmt: skip
        return out


def _odds_outcome(market: str, name: str | None, ev: dict) -> str | None:
    if market == "totals":
        return (name or "").lower() if (name or "").lower() in ("over", "under") else None
    if name == ev.get("home_team"):
        return "home"
    return "away" if name == ev.get("away_team") else None


# --- Kalshi --------------------------------------------------------------------------------------


class KalshiFeed:
    def __init__(self, http: Http):
        self.http = http

    def winners(self, conn: Connection, sport: str) -> list[Quote]:
        """Open game-winner markets: the yes ask as an American price for each team."""
        r = self.http.get("kalshi", f"{KALSHI_BASE}/markets",
                          params={"series_ticker": KALSHI_SERIES[sport], "status": "open",
                                  "limit": 1000})  # fmt: skip
        r.raise_for_status()
        abbr = {a: tid for tid, a in conn.execute(select(teams.c.team_id, teams.c.abbrev).where(
            teams.c.sport_id == sport_id(conn, sport))).all()}  # fmt: skip
        alias = KALSHI_ALIAS.get(sport, {})
        out = []
        for m in r.json().get("markets") or []:
            parsed = parse_kalshi_ticker(m.get("ticker", ""), set(abbr) | set(alias))
            ask = _num(m.get("yes_ask_dollars"))
            if parsed is None or ask is None or not 0 < ask < 1:
                continue
            day, t1, t2, team = (parsed[0], *(alias.get(x, x) for x in parsed[1:]))
            gid = _game_on(conn, sport, abbr.get(t1), abbr.get(t2), day)
            if gid is None:
                continue
            home = conn.execute(select(games.c.home_team_id).where(
                games.c.game_id == gid)).scalar()  # fmt: skip
            side = "home" if abbr.get(team) == home else "away"
            out.append(Quote(sport, gid, "kalshi", "kalshi", "h2h", side, None,
                             prob_to_american(ask)))
        return out


def parse_kalshi_ticker(ticker: str, known: set[str]):
    """'KXNBAGAME-26OCT26GSWDEN-GSW' -> (date 2026-10-26, 'GSW', 'DEN', 'GSW'); team codes of
    two or three letters are split where both halves are known."""
    parts = ticker.split("-")
    if len(parts) != 3 or len(parts[1]) < 11:
        return None
    try:
        day = datetime.strptime(parts[1][:7].title(), "%y%b%d").date()
    except ValueError:
        return None
    pair = parts[1][7:]
    for i in (2, 3, 4):
        if pair[:i] in known and pair[i:] in known:
            return day, pair[:i], pair[i:], parts[2]
    return None


def _game_on(conn, sport, t1, t2, day) -> int | None:
    from betdata.alerts.rules import EASTERN

    if t1 is None or t2 is None:
        return None
    rows = conn.execute(select(games.c.game_id, games.c.start_time_utc).where(
        games.c.sport_id == sport_id(conn, sport),
        ((games.c.home_team_id == t1) & (games.c.away_team_id == t2))
        | ((games.c.home_team_id == t2) & (games.c.away_team_id == t1)))).all()  # fmt: skip
    for r in rows:
        if _aware(r.start_time_utc).astimezone(EASTERN).date() == day:
            return r.game_id
    return None


def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def prob_to_american(p: float) -> int:
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)
