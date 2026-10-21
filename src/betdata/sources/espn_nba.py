"""ESPN's public NBA APIs (free, no key): the live schedule, scores and lines, box scores for the
current season, and historical opening/closing lines.

  scoreboard     site.api.espn.com .../scoreboard?dates=YYYYMMDD-YYYYMMDD
                 Games, tip times, status, final scores, and the DraftKings line ESPN shows
                 (current spread, moneyline, total, plus the opening values). The NBA schedule
                 authority on GitHub Actions, where cdn.nba.com answers 403.
  boxscores      .../summary?event=ID for finished games that have no box score yet: players'
                 minutes, points, plus/minus, starter, did-not-play (the current season has no
                 sportsdataverse file until it ends). The plays are kept too and rebuilt into
                 lineup stints (`nba_stints`, M15).
  stints         .../summary?event=ID again for finished games that have a box score but no
                 stints (games fetched before M15); stores stints only.
  odds_history   sports.core.api.espn.com .../events/ID/competitions/ID/odds for past games:
                 each book's opening and last (closing) line, back to 2015-16 (MODEL.md).

Summaries (~350 KB) and per-game odds are reduced to the fields we keep before they are archived,
so the archive holds one compact JSON body per run rather than thousands of large ones.
Lines: the scoreboard's current values are `poll` snapshots; opening values `source_opening`;
historical last values `source_closing` (never a model input).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, select

from betdata.config import SourceConfig
from betdata.core import raw_store
from betdata.core.errors import ApiError, ParseError
from betdata.core.http import SourceClient
from betdata.core.matching import sport_id
from betdata.core.models import ParsedEvent, ParsedOdds, ParsedPrice
from betdata.core.normalize.odds_math import american_to_decimal
from betdata.core.odds_loader import load_odds
from betdata.core.results_loader import load_results
from betdata.core.source import JobSpec, RawInput, Source
from betdata.db.schema import game_source_ids, games, nba_player_games, nba_stints, odds_snapshots

NAME = "espn_nba"
log = logging.getLogger(__name__)
EASTERN = ZoneInfo("America/New_York")
SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
STATUS = {"STATUS_FINAL": "final", "STATUS_SCHEDULED": "scheduled",
          "STATUS_POSTPONED": "postponed", "STATUS_CANCELED": "cancelled",
          "STATUS_CANCELLED": "cancelled"}  # fmt: skip
SEASON_TYPES = {1: "PRE", 2: "REG", 3: "POST", 5: "POST"}  # 5 = play-in
# ESPN provider names -> our book keys. Pseudo-providers (consensus, model sites) are skipped;
# "Opening" (2015-2019) is the market's opening line and is kept as book `opening`.
BOOKS = {
    "draftkings": "draftkings", "espn bet": "espnbet", "fanduel": "fanduel", "mgm": "betmgm",
    "betmgm": "betmgm", "caesars sportsbook (new jersey)": "caesars", "caesars": "caesars",
    "william hill (new jersey)": "caesars", "bovada.lv": "bovada", "betonline.ag": "betonline",
    "5dimes.eu": "5dimes", "sportsbetting.ag": "sportsbetting", "westgate": "westgate",
    "betrivers": "betrivers", "unibet": "unibet", "sugarhouse": "sugarhouse",
    "pointsbet": "pointsbet", "opening": "opening",
}  # fmt: skip
BOOKS_NORM = {re.sub(r"[^a-z0-9()]", "", k): v for k, v in BOOKS.items()}


def book_of(provider: str) -> str | None:
    """Our book key for an ESPN provider name, ignoring case, spaces and punctuation
    ("DraftKings" and "Draft Kings" are the same book)."""
    key = re.sub(r"[^a-z0-9()]", "", str(provider).lower())
    return BOOKS_NORM.get(key)


def _int_odds(v) -> int | None:
    if v in (None, "", "OFF", "EVEN", "even"):
        return 100 if str(v).lower() == "even" else None
    try:
        x = float(str(v).replace("+", ""))
    except ValueError:
        return None
    return int(round(x)) if abs(x) >= 100 else None


def _line(v) -> float | None:
    if v is None:
        return None
    s = str(v).lstrip("ou").replace("+", "")
    if s.upper() in ("PK", "PICK", "EVEN"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def scoreboard_days(dates: str | None, today: date) -> list[date]:
    """'20261020' or '20261020-20261023' (inclusive) or None (yesterday to 2 days ahead)."""
    if not dates:
        return [today + timedelta(days=k) for k in range(-1, 3)]
    first, _, last = str(dates).partition("-")
    a = datetime.strptime(first, "%Y%m%d").date()
    b = datetime.strptime(last, "%Y%m%d").date() if last else a
    return [a + timedelta(days=k) for k in range((b - a).days + 1)]


def _price(gid, book, market, outcome, line, price, kind) -> ParsedPrice | None:
    if price is None and market == "h2h":
        return None
    if market != "h2h" and line is None:
        return None
    return ParsedPrice(gid, book, None, market, outcome, line if market != "h2h" else None,
                       price, american_to_decimal(price) if price else None,
                       snapshot_kind=kind)  # fmt: skip


# --- scoreboard --------------------------------------------------------------------------


def _event(e: dict) -> ParsedEvent | None:
    c = e["competitions"][0]
    side = {x["homeAway"]: x for x in c["competitors"]}
    if "home" not in side or "away" not in side:
        return None
    name = e["status"]["type"]["name"]
    status = STATUS.get(name, "in_progress" if e["status"]["type"].get("state") == "in"
                        else "scheduled")  # fmt: skip
    final = status == "final"
    score = {k: int(float(v["score"])) if final and v.get("score") not in (None, "") else None
             for k, v in side.items()}  # fmt: skip
    season = e.get("season") or {}
    start = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
    return ParsedEvent(
        source_game_id=str(e["id"]), start_time_utc=start,
        home_name=side["home"]["team"]["displayName"],
        away_name=side["away"]["team"]["displayName"],
        neutral_site=bool(c.get("neutralSite")),
        season=int(season["year"]) - 1 if season.get("year") else None,
        season_type=SEASON_TYPES.get(season.get("type")),
        home_score=score["home"], away_score=score["away"], final=final, status=status,
        extra_ids={"espn": str(e["id"])},
    )  # fmt: skip


def _scoreboard_prices(gid: str, odds: dict) -> list[ParsedPrice]:
    book = book_of(odds.get("provider", {}).get("name", ""))
    if book is None:
        return []
    out = []
    for kind, which in (("poll", "close"), ("source_opening", "open")):
        ml, ps, tot = odds.get("moneyline") or {}, odds.get("pointSpread") or {}, odds.get(
            "total") or {}
        for side in ("home", "away"):
            out.append(_price(gid, book, "h2h", side, None,
                              _int_odds(((ml.get(side) or {}).get(which) or {}).get("odds")),
                              kind))  # fmt: skip
            s = (ps.get(side) or {}).get(which) or {}
            out.append(_price(gid, book, "spreads", side, _line(s.get("line")),
                              _int_odds(s.get("odds")), kind))  # fmt: skip
        for side in ("over", "under"):
            t = (tot.get(side) or {}).get(which) or {}
            out.append(_price(gid, book, "totals", side, _line(t.get("line")),
                              _int_odds(t.get("odds")), kind))  # fmt: skip
    return [p for p in out if p is not None]


def parse_scoreboard(body: bytes, fetched_at: datetime) -> ParsedOdds:
    try:
        data = json.loads(body)
        out = ParsedOdds(sport="nba", fetched_at=fetched_at, schedule_authority=True,
                         requested_markets=("h2h", "spreads", "totals"))  # fmt: skip
        for e in data.get("events", []):
            ev = _event(e)
            if ev is None:
                continue
            out.events.append(ev)
            if ev.status == "scheduled":  # pre-game lines only
                for odds in e["competitions"][0].get("odds") or []:
                    out.prices.extend(_scoreboard_prices(ev.source_game_id, odds))
    except (ValueError, KeyError, TypeError, IndexError) as e:
        raise ParseError(f"{NAME}: unexpected scoreboard shape: {e!r}") from e
    return out


# --- box scores --------------------------------------------------------------------------


def summary_box(espn_id: str, data: dict) -> dict:
    """The compact box score of one game summary."""
    comp = data["header"]["competitions"][0]
    out = {"espn_game_id": str(espn_id), "tip": comp["date"],
           "status": comp["status"]["type"]["name"], "players": []}  # fmt: skip
    for team in data.get("boxscore", {}).get("players", []):
        name = team["team"]["displayName"]
        stats = (team.get("statistics") or [{}])[0]
        keys = stats.get("keys") or []
        for a in stats.get("athletes", []):
            vals = dict(zip(keys, a.get("stats") or [], strict=False))

            def num(k, vals=vals):
                try:
                    return float(str(vals.get(k)).replace("+", ""))
                except (TypeError, ValueError):
                    return None

            def pair(k, vals=vals):
                try:
                    made, att = str(vals.get(k)).split("-")
                    return float(made), float(att)
                except (TypeError, ValueError):
                    return None, None

            fgm, fga = pair("fieldGoalsMade-fieldGoalsAttempted")
            fg3m, fg3a = pair("threePointFieldGoalsMade-threePointFieldGoalsAttempted")
            ftm, fta = pair("freeThrowsMade-freeThrowsAttempted")
            out["players"].append(dict(
                team=name, player_id=str(a["athlete"]["id"]),
                player_name=a["athlete"]["displayName"], starter=bool(a.get("starter")),
                did_not_play=bool(a.get("didNotPlay")), reason=(a.get("reason") or None),
                minutes=num("minutes"), points=num("points"), plus_minus=num("plusMinus"),
                fgm=fgm, fga=fga, fg3m=fg3m, fg3a=fg3a, ftm=ftm, fta=fta,
                oreb=num("offensiveRebounds"), dreb=num("defensiveRebounds"),
                ast=num("assists"), stl=num("steals"), blk=num("blocks"), tov=num("turnovers"),
                pf=num("fouls")))  # fmt: skip
    return out


def summary_plays(data: dict) -> dict:
    """The fields of a summary's plays that lineup stints need, compact: [sequence, period,
    clock, type, team id, athlete ids (substitutions: entering, leaving), home score, away
    score, shooting], plus ESPN's home team id."""
    comp = data["header"]["competitions"][0]
    home = next((str(c["team"]["id"]) for c in comp.get("competitors", [])
                 if c.get("homeAway") == "home"), None)  # fmt: skip
    plays = []
    for p in data.get("plays") or []:
        team = (p.get("team") or {}).get("id")
        plays.append([
            int(p.get("sequenceNumber") or 0), int((p.get("period") or {}).get("number") or 0),
            str((p.get("clock") or {}).get("displayValue") or "0"),
            str((p.get("type") or {}).get("text") or "").strip(),
            None if team is None else str(team),
            [str(a["athlete"]["id"]) for a in p.get("participants") or []
             if (a.get("athlete") or {}).get("id") is not None],
            p.get("homeScore"), p.get("awayScore"), bool(p.get("shootingPlay"))])  # fmt: skip
    return {"espn_home": home, "plays": plays}


def stint_plays(body: bytes) -> dict:
    """{ESPN game id: (ESPN home team id, plays)} for finished games whose plays were kept."""
    from betdata.nba.stints import Play, clock_seconds

    out = {}
    for g in json.loads(body).get("games", []):
        if g.get("status") != "STATUS_FINAL" or not g.get("plays"):
            continue
        out[g["espn_game_id"]] = (g.get("espn_home"), [
            Play(seq, period, clock_seconds(clock), kind, team, tuple(ids),
                 None if hs is None else int(hs), None if as_ is None else int(as_), shot)
            for seq, period, clock, kind, team, ids, hs, as_, shot in g["plays"]])  # fmt: skip
    return out


def parse_boxscores(body: bytes) -> list[dict]:
    rows = []
    for g in json.loads(body).get("games", []):
        if g.get("status") != "STATUS_FINAL":
            continue
        tip = datetime.fromisoformat(g["tip"].replace("Z", "+00:00"))
        rows.extend({**p, "espn_game_id": g["espn_game_id"], "tip": tip} for p in g["players"])
    return rows


# --- historical odds ---------------------------------------------------------------------


def core_odds(espn_id: str, data: dict) -> dict:
    """Each book's last and opening line for one game, compact."""
    books = []
    for it in data.get("items", []):
        name = str((it.get("provider") or {}).get("name", "")).strip()
        if "live" in name.lower():
            continue
        home, away = it.get("homeTeamOdds") or {}, it.get("awayTeamOdds") or {}

        def open_line(side):
            o = side.get("open") or {}
            return (o.get("pointSpread") or {}).get("american")

        def open_ml(side):
            o = side.get("open") or {}
            return (o.get("moneyLine") or {}).get("american")

        books.append(dict(
            provider=name, spread=it.get("spread"), total=it.get("overUnder"),
            spread_home_price=home.get("spreadOdds"), spread_away_price=away.get("spreadOdds"),
            ml_home=home.get("moneyLine"), ml_away=away.get("moneyLine"),
            open_spread=open_line(home), open_ml_home=open_ml(home), open_ml_away=open_ml(away),
            open_total=((it.get("open") or {}).get("total") or {}).get("american")))
        # fmt: skip
    return {"espn_game_id": str(espn_id), "books": books}


def parse_odds_history(body: bytes, fetched_at: datetime) -> ParsedOdds:
    """Closing (last) lines as source_closing, opening lines as source_opening. The `opening`
    pseudo-book's values are openers."""
    data = json.loads(body)
    out = ParsedOdds(sport="nba", fetched_at=fetched_at)
    for g in data.get("games", []):
        gid = g["espn_game_id"]
        for b in g["books"]:
            book = book_of(b["provider"])
            if book is None:
                continue
            kind = "source_opening" if book == "opening" else "source_closing"
            hp, ap = _int_odds(b.get("spread_home_price")), _int_odds(b.get("spread_away_price"))
            spread = _line(b.get("spread"))
            items = [
                _price(gid, book, "spreads", "home", spread, hp, kind),
                _price(gid, book, "spreads", "away", None if spread is None else -spread, ap,
                       kind),
                _price(gid, book, "h2h", "home", None, _int_odds(b.get("ml_home")), kind),
                _price(gid, book, "h2h", "away", None, _int_odds(b.get("ml_away")), kind),
                _price(gid, book, "totals", "over", _line(b.get("total")), None, kind),
            ]  # fmt: skip
            if book != "opening":
                osp = _line(b.get("open_spread"))
                items += [
                    _price(gid, book, "spreads", "home", osp, None, "source_opening"),
                    _price(gid, book, "h2h", "home", None, _int_odds(b.get("open_ml_home")),
                           "source_opening"),
                    _price(gid, book, "h2h", "away", None, _int_odds(b.get("open_ml_away")),
                           "source_opening"),
                    _price(gid, book, "totals", "over", _line(b.get("open_total")), None,
                           "source_opening"),
                ]  # fmt: skip
            out.prices.extend(p for p in items if p is not None)
    return out


class EspnNbaSource(Source):
    name = NAME

    def __init__(self, engine, config: SourceConfig, *, transport=None, sleep=None, clock=None):
        super().__init__(engine, config, clock)
        extra = {k: v for k, v in dict(sleep=sleep, clock=clock).items() if v is not None}
        interval = config.options.get("min_interval_seconds", 0.0)
        self.client = SourceClient(engine, NAME, config.options.get("base_url", SITE),
                                   transport=transport, min_interval=interval, **extra)
        self.core = SourceClient(engine, NAME, config.options.get("core_url", CORE),
                                 transport=transport, min_interval=interval, label="core",
                                 **extra)  # fmt: skip

    def close(self) -> None:
        self.client.close()
        self.core.close()

    @classmethod
    def jobs(cls) -> dict[str, JobSpec]:
        return {
            "scoreboard": JobSpec("scoreboard", "Games, scores and DraftKings lines (--dates "
                                  "20261020-20261023; default yesterday to 2 days ahead)"),
            "boxscores": JobSpec("boxscores", "Box scores of finished games that have none "
                                 "(--days 3)"),
            "stints": JobSpec("stints", "Lineup stints of finished games with a box score "
                              "but none (--days 3)"),
            "odds_history": JobSpec("odds_history", "Opening and closing lines of past games "
                                    "(--season 2023 --limit 500)"),
        }  # fmt: skip

    def _today(self):
        return self.clock().astimezone(EASTERN).date()

    def fetch(self, job: str, params: dict[str, Any], *, priority: str, run_id: int) -> RawInput:
        if job == "scoreboard":
            # One request per day: the API answers 400 to a date range (seen on Actions).
            days = scoreboard_days(params.get("dates"), self._today())
            events: dict = {}
            for day in days:
                resp = self.client.get("/scoreboard", {"dates": f"{day:%Y%m%d}", "limit": 200},
                                       est_cost=0, run_id=run_id, store_raw=False)  # fmt: skip
                for e in json.loads(resp.body).get("events", []):
                    events[str(e.get("id"))] = e  # a game listed on two days counts once
            body = json.dumps({"events": list(events.values())}).encode()
            at = self.clock()
            with self.engine.begin() as conn:
                raw_id = raw_store.save(conn, source=NAME, endpoint="/scoreboard",
                                        params={"dates": [f"{d:%Y%m%d}" for d in days]},
                                        http_status=200, fetched_at=at, body=body,
                                        content_type="application/json")  # fmt: skip
            return RawInput("/scoreboard", None, body, at, raw_id)
        if job in ("boxscores", "stints"):
            box = job == "boxscores"
            days = int(params.get("days", 3))
            ids = self._needing_box(days) if box else self._needing_stints(days)
            out = []
            for espn_id in ids:
                try:
                    resp = self.client.get("/summary", {"event": espn_id}, est_cost=0,
                                           run_id=run_id, store_raw=False)  # fmt: skip
                    data = json.loads(resp.body)
                    game = summary_box(espn_id, data)
                    if not box:  # stints only: the stored box score stays as it is
                        game["players"] = []
                    out.append({**game, **summary_plays(data)})
                except (ApiError, ValueError, KeyError, TypeError) as e:
                    log.warning("%s summary %s: %s", NAME, espn_id, e)
            return self._compact("/summary" if box else "/summary/stints", {"games": out},
                                 run_id)  # fmt: skip
        if job == "odds_history":
            ids = self._needing_odds(int(params["season"]), int(params.get("limit", 500)))
            out = []
            for espn_id in ids:
                try:
                    resp = self.core.get(f"/events/{espn_id}/competitions/{espn_id}/odds",
                                         {"limit": 50}, est_cost=0, run_id=run_id,
                                         store_raw=False)  # fmt: skip
                    out.append(core_odds(espn_id, json.loads(resp.body)))
                except ApiError as e:
                    if e.status != 404:
                        raise
                    out.append({"espn_game_id": espn_id, "books": []})  # no lines kept
            return self._compact("/odds", {"games": out}, run_id)
        raise KeyError(job)

    def _compact(self, endpoint: str, payload: dict, run_id: int) -> RawInput:
        body = json.dumps(payload, default=str).encode()
        at = self.clock()
        with self.engine.begin() as conn:
            raw_id = raw_store.save(conn, source=NAME, endpoint=endpoint, params=None,
                                    http_status=200, fetched_at=at, body=body,
                                    content_type="application/json")  # fmt: skip
        return RawInput(endpoint, None, body, at, raw_id)

    def _espn_ids(self, conn, *where):
        q = (select(game_source_ids.c.source_game_id)
             .join(games, games.c.game_id == game_source_ids.c.game_id)
             .where(game_source_ids.c.source == "espn",
                    games.c.sport_id == sport_id(conn, "nba"), *where)
             .order_by(games.c.start_time_utc))  # fmt: skip
        return list(conn.execute(q).scalars())

    def _needing_box(self, days: int) -> list[str]:
        now = self.clock()
        with self.engine.connect() as conn:
            return self._espn_ids(
                conn, games.c.status == "final",
                games.c.start_time_utc >= now - timedelta(days=days),
                ~exists().where(nba_player_games.c.game_id == games.c.game_id))

    def _needing_stints(self, days: int) -> list[str]:
        now = self.clock()
        with self.engine.connect() as conn:
            return self._espn_ids(
                conn, games.c.status == "final",
                games.c.start_time_utc >= now - timedelta(days=days),
                exists().where(nba_player_games.c.game_id == games.c.game_id),
                ~exists().where(nba_stints.c.game_id == games.c.game_id))

    def _needing_odds(self, season: int, limit: int) -> list[str]:
        """Past games of the season with no stored line from this source yet (resumable)."""
        now = self.clock()
        with self.engine.connect() as conn:
            done = exists().where(and_(odds_snapshots.c.game_id == games.c.game_id,
                                       odds_snapshots.c.source == NAME,
                                       odds_snapshots.c.snapshot_kind != "poll"))
            tried = select(game_source_ids.c.source_game_id).where(
                game_source_ids.c.source == f"{NAME}_odds_tried")
            ids = self._espn_ids(conn, games.c.season == season, games.c.status == "final",
                                 games.c.start_time_utc < now - timedelta(hours=6), ~done,
                                 game_source_ids.c.source_game_id.not_in(tried))  # fmt: skip
        return ids[:limit]

    def job_for_endpoint(self, endpoint: str) -> str:
        return {"/scoreboard": "scoreboard", "/summary": "boxscores",
                "/summary/stints": "stints", "/odds": "odds_history"}[endpoint]  # fmt: skip

    def parse(self, job: str, raw: RawInput):
        if job == "scoreboard":
            return parse_scoreboard(raw.body, raw.fetched_at)
        if job in ("boxscores", "stints"):
            return parse_boxscores(raw.body), stint_plays(raw.body)
        return (parse_odds_history(raw.body, raw.fetched_at), json.loads(raw.body))

    def load(self, conn, job: str, batch, *, run_id, raw_id) -> tuple[int, int]:
        if job == "scoreboard":
            stats = load_odds(conn, source=NAME, batch=batch, run_id=run_id, raw_id=raw_id)
            changed = load_results(conn, source=NAME, events=batch.events,
                                   matches=stats.matches)  # fmt: skip
            return stats.inserted + changed, len(batch.events)
        if job in ("boxscores", "stints"):
            from betdata.nba.box import load_box_rows
            from betdata.nba.stints import load_stints

            rows, plays = batch
            n = 0
            if rows and job == "boxscores":
                n, _ = load_box_rows(conn, rows, source=NAME, fetched_at=self.clock(), live=True)
            if plays:  # after the box score: it gives the starters
                res = load_stints(conn, plays, source=NAME, fetched_at=self.clock(), live=True)
                if job == "stints":
                    return res.stints, len(plays)
            return n, len(rows)
        parsed, payload = batch
        # Odds rows point at games through the ESPN id the schedule recorded.
        from betdata.nba.box import espn_games

        ids = espn_games(conn, (g["espn_game_id"] for g in payload["games"]))
        tried = [g["espn_game_id"] for g in payload["games"] if not g["books"]]
        if tried:
            from sqlalchemy import insert

            conn.execute(insert(game_source_ids), [
                dict(source=f"{NAME}_odds_tried", source_game_id=t, game_id=ids[t],
                     home_away_swapped=False) for t in tried if t in ids])  # fmt: skip
        inserted = _load_history(conn, parsed, ids, run_id=run_id, raw_id=raw_id)
        return inserted, len(payload["games"])


def _load_history(conn, parsed: ParsedOdds, ids: dict[str, int], *, run_id, raw_id) -> int:
    """Historical snapshots go straight in, keyed to the game by its ESPN id: there is no event
    list to match teams on, and the schedule already linked every ESPN id to its game."""
    from sqlalchemy import insert

    from betdata.core.normalize.catalog import BookResolver, MarketResolver

    books, markets = BookResolver(conn, NAME), MarketResolver(conn)
    rows, seen = [], set()
    for p in parsed.prices:
        gid = ids.get(p.source_game_id)
        if gid is None:
            continue
        key = (p.source_game_id, p.book_key, p.market_key, p.outcome, p.snapshot_kind)
        if key in seen:
            continue
        seen.add(key)
        rows.append(dict(source=NAME, source_game_id=p.source_game_id, game_id=gid,
                         book_id=books.resolve(p.book_key, p.book_title),
                         market_id=markets.resolve(p.market_key, p.period), outcome=p.outcome,
                         line=p.line, price_american=p.price_american,
                         price_decimal=p.price_decimal, price_status="active",
                         snapshot_kind=p.snapshot_kind, fetched_at=parsed.fetched_at,
                         run_id=run_id, raw_id=raw_id))  # fmt: skip
    for i in range(0, len(rows), 500):
        conn.execute(insert(odds_snapshots), rows[i:i + 500])
    return len(rows)


__all__ = ["EspnNbaSource", "UTC"]
