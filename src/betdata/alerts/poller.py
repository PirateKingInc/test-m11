"""The M16 poller (Part C): one clock-hour job that polls injury news and prices faster than the
hourly page, re-predicts only the affected game with the frozen models, and raises alerts.

It runs as its own job in the clock workflow, on a throwaway copy of the hourly state (restored
read-only), and keeps what it learns in `data/poller.sqlite` (its own cache key). It never
refits a model and never writes the hourly state. It does not overlap the hourly job: every
cycle first asks GitHub whether an hourly run is queued or in progress, and skips if so.

Cadence (MODEL.md, PROJECT.md "M16 poller"):
- NBA: the newest official report every 3 minutes in report windows (game days 10:00-19:30 ET,
  and 16:30-20:30 ET the day before a game day); at most one request a second.
- NFL: every 5 minutes from 120 to 60 minutes before each kickoff window (`nfl.py`).
- Lag study: each game with a qualifying change is polled at its sport's cadence for 60 minutes.
- Signal 2: scheduled Pinnacle boards within the credit budget (see `sharp_boards`).
- Closing polls: games with alerts, in the 10 minutes before tip.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import Engine, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from betdata.alerts import rules as R
from betdata.alerts.db import (
    alerts,
    get_state,
    poller_runs,
    quotes,
    repredictions,
    set_state,
    status_events,
)
from betdata.alerts.feeds import Http, KalshiFeed, PinnacleFeed, Quote, SharpFeed
from betdata.alerts.notify import Alert, emit
from betdata.db.schema import games, teams

log = logging.getLogger(__name__)
NBA_CADENCE = timedelta(minutes=3)
NFL_CADENCE = timedelta(minutes=5)
CLOSING_MIN = 10
# Signal 2 boards (each 3 credits): NBA at 17:30 and 18:45 ET on game days with a tip in the
# next 3 hours; NFL once per kickoff window, 75 minutes before it. ~220 credits a month.
NBA_BOARDS_ET = ((17, 30), (18, 45))
NFL_BOARD_BEFORE = timedelta(minutes=75)


def utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def hourly_running(client: httpx.Client | None = None) -> bool:
    """True while an hourly run is queued or in progress (the GitHub API, with the job token).
    Without a token (local runs, tests) there is nothing to wait for."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        return False
    c = client or httpx.Client(timeout=15)
    for status in ("in_progress", "queued"):
        try:
            r = c.get(f"https://api.github.com/repos/{repo}/actions/workflows/hourly.yml/runs",
                      params={"status": status, "per_page": 1},
                      headers={"Authorization": f"Bearer {token}"})  # fmt: skip
            if r.status_code == 200 and r.json().get("total_count", 0) > 0:
                return True
        except httpx.HTTPError:
            return True  # unsure: stay out of the hourly job's way this cycle
    return False


@dataclass
class Keys:
    sharpapi: str | None = None
    odds_api: str | None = None
    ntfy_topic: str | None = None

    @classmethod
    def from_env(cls) -> Keys:
        return cls(os.environ.get("SHARPAPI_KEY") or None,
                   os.environ.get("ODDS_API_KEY") or os.environ.get("THE_ODDS_API_KEY") or None,
                   os.environ.get("NTFY_TOPIC") or None)  # fmt: skip


@dataclass
class Poller:
    main: Engine  # the throwaway copy of the hourly state
    pdb: Engine  # data/poller.sqlite
    keys: Keys
    http: Http = field(default_factory=Http)
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep
    lock: Callable[[], bool] = hourly_running
    push_client: httpx.Client | None = None
    nfl_handler: object | None = None  # betdata.alerts.nfl.NflHandler when available
    _nba_models: object | None = None
    run_id: int | None = None

    def __post_init__(self):
        self.sharp = SharpFeed(self.http, self.keys.sharpapi) if self.keys.sharpapi else None
        self.pin = PinnacleFeed(self.http, self.keys.odds_api) if self.keys.odds_api else None
        self.kalshi = KalshiFeed(self.http)
        self.cycles = self.skipped = 0
        self.errors: list[str] = []
        self.notes: list[str] = []

    # --- the loop ---------------------------------------------------------------------------

    def run(self, until: datetime) -> int:
        with self.pdb.begin() as p:
            self.run_id = p.execute(insert(poller_runs).values(
                started_at=self.now_fn(), status="running")).inserted_primary_key[0]  # fmt: skip
        status = "ok"
        try:
            self.grade(self.now_fn())
            while (now := self.now_fn()) < until:
                if not self.has_work(now, until):  # nothing can happen before :56
                    self.notes.append("idle: no window, lag poll, board or close before :56")
                    break
                if not self.has_work(now, now + timedelta(minutes=1)):
                    pass  # nothing due this minute: no need to ask about the hourly job
                elif self.lock():
                    self.skipped += 1
                else:
                    self.cycles += 1
                    self.cycle(now)
                self._save_run("running")
                nxt = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
                self.sleep(max(0.0, (min(nxt, until) - self.now_fn()).total_seconds()))
        except Exception as e:  # recorded; the job's own step is allowed to fail
            status = "failed"
            self.errors.append(f"{type(e).__name__}: {e}")
            raise
        finally:
            self._save_run(status)
        return self.cycles

    def _save_run(self, status: str) -> None:
        with self.pdb.begin() as p:
            p.execute(update(poller_runs).where(poller_runs.c.run_id == self.run_id).values(
                status=status, cycles=self.cycles, skipped_cycles=self.skipped,
                requests=dict(self.http.count),
                message="; ".join((self.notes + self.errors)[-5:]) or None,
                finished_at=None if status == "running" else self.now_fn()))  # fmt: skip

    def _guard(self, what: str, fn, *args):
        """One task's failure is logged and recorded; the other tasks still run."""
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            log.exception("%s failed", what)
            self.errors.append(f"{what}: {type(e).__name__}: {str(e)[:200]}")
            return None

    def _due(self, key: str, every: timedelta, now: datetime) -> bool:
        with self.pdb.begin() as p:
            last = get_state(p, key)
            if last and now - datetime.fromisoformat(last) < every - timedelta(seconds=5):
                return False
            set_state(p, key, now.isoformat(), now)
        return True

    def has_work(self, start: datetime, end: datetime) -> bool:
        """Could any task be due between start and end (checked every 5 minutes)?"""
        from betdata.alerts import nba

        with self.pdb.begin() as p:
            if any(datetime.fromisoformat(w["until"]) > start
                   for w in get_state(p, "lag:open", {}).values()):  # fmt: skip
                return True
            if p.execute(select(alerts.c.alert_id).where(
                    alerts.c.tip > start,
                    alerts.c.tip <= end + timedelta(minutes=CLOSING_MIN))).first():  # fmt: skip
                return True
        t = start
        with self.main.connect() as c:
            while t <= end:
                if nba.active(c, t) or self.board_times(t, exact=False):
                    return True
                if self.nfl_handler is not None and self.nfl_handler.active(c, t):
                    return True
                t += timedelta(minutes=5)
        return False

    def cycle(self, now: datetime) -> None:
        from betdata.alerts import nba

        with self.main.connect() as c:
            nba_on = nba.active(c, now)
        if nba_on and self._due("due:nba_report", NBA_CADENCE, now):
            self._guard("nba report", self.nba_report, now)
        if self.nfl_handler is not None:
            self._guard("nfl status", self.nfl_handler.cycle, self, now)
        self._guard("lag polls", self.lag_polls, now)
        self._guard("sharp boards", self.sharp_boards, now)
        self._guard("closing polls", self.closing_polls, now)

    # --- NBA news ---------------------------------------------------------------------------

    def nba_models(self, now: datetime):
        from betdata.alerts.nba import Models

        if self._nba_models is None:
            self._nba_models = Models(self.main, now)
        return self._nba_models

    def nba_report(self, now: datetime) -> int:
        from betdata.alerts import nba

        with self.pdb.begin() as p:
            last = get_state(p, "nba:last_report")
        found = nba.newest_report(self.http, now, last)
        if found is None:
            return 0
        name, pdf = found
        report = nba.load_new_report(self.main, name, pdf, now)
        with self.pdb.begin() as p:
            set_state(p, "nba:last_report", name, now)
        models = self.nba_models(now)
        store = models.store(self.main, now)
        changes = nba.detect(self.main, store, report, now)
        by_game: dict[int, list[tuple[int, nba.Change]]] = {}
        for ch in changes:
            event_id = self.record_event("nba", ch.game_id, ch.player_key, ch.player_name,
                                         ch.team_id, ch.old_status, ch.new_status,
                                         ch.prob_before, ch.prob_after, ch.min_play, None,
                                         ch.qualifies, ch.report_name, ch.report_at, now, ch.tip,
                                         {"known_at": ch.known_at.isoformat()})  # fmt: skip
            if event_id is not None and ch.qualifies:
                by_game.setdefault(ch.game_id, []).append((event_id, ch))
        with self.pdb.begin() as p:  # a short log of every report read (verification)
            log_ = get_state(p, "nba:reports", [])[-49:]
            log_.append({"name": name, "stated": report.report_at.isoformat(),
                         "known": report.known_at.isoformat(), "read": now.isoformat(),
                         "parsed": report.parsed, "inserted": report.inserted,
                         "changes": len(changes),
                         "qualifying": sum(c.qualifies for c in changes)})  # fmt: skip
            set_state(p, "nba:reports", log_, now)
        for gid, evs in by_game.items():
            self._guard(f"nba game {gid}", self.nba_game, store, models, gid, evs, report, now)
        return len(changes)

    def record_event(self, sport, game_id, player_key, player_name, team_id, old, new, pb, pa,
                     weight, position, qualifies, report_name, report_at, now, tip,
                     details=None) -> int | None:  # fmt: skip
        stmt = sqlite_insert(status_events).values(
            sport=sport, game_id=game_id, player_key=player_key, player_name=player_name,
            team_id=team_id, old_status=old, new_status=new, prob_before=pb, prob_after=pa,
            weight=weight, position=position, qualifies=qualifies, report_name=report_name,
            report_at=report_at, detected_at=now, tip=tip, details=details,
        ).on_conflict_do_nothing()  # fmt: skip
        with self.pdb.begin() as p:
            res = p.execute(stmt)
            return res.inserted_primary_key[0] if res.rowcount else None

    def nba_game(self, store, models, gid: int, evs: list, report, now: datetime) -> None:
        first_event, ch = evs[0]
        rp = models.repredict(self.main, store, ch, now)
        with self.pdb.begin() as p:
            p.execute(insert(repredictions).values(
                event_id=first_event, game_id=gid, model_version=rp.model_version,
                point=rp.point, margin_before=rp.margin_before, margin_after=rp.margin_after,
                total_before=rp.total_before, total_after=rp.total_after,
                props=list(rp.props), created_at=now))  # fmt: skip
        self.open_lag("nba", gid, first_event, now)
        qs = self.poll_game("nba", gid, now, first_event)
        players = {c.player_name: c for _, c in evs}
        why = "; ".join(f"{c.player_name} {c.old_status or 'not listed'} -> "
                        f"{c.new_status or 'not listed'}" for _, c in evs)  # fmt: skip
        stated = report.report_at.astimezone(R.EASTERN).strftime("%-I:%M %p ET")
        reason = f"{why} (report {stated})"
        self.signal_news("nba", gid, first_event, report.name, rp, qs, players, reason, now,
                         models.p_over)

    # --- Signal 1 -----------------------------------------------------------------------------

    def signal_news(self, sport, gid, event_id, change_key, rp, qs: list[Quote], players: dict,
                    reason: str, now: datetime, p_over_fn) -> list[int]:  # fmt: skip
        with self.pdb.begin() as p:
            if get_state(p, "signal1:off"):
                return []
            decided = get_state(p, "signal1:decision")
        study = decided is None
        label, tip = self.game_label(gid)
        out = []
        spreads = _spreads(qs)
        pick = R.side_signal(sport, rp.margin_before, rp.margin_after, spreads)
        if pick is not None:
            a = Alert("news", sport, gid, label, tip, "spreads", pick.side, pick.line, pick.price,
                      pick.book, f"{reason}; model margin {rp.margin_before:+.1f} -> "
                      f"{rp.margin_after:+.1f} (home), edge {pick.edge:.1f} pts at "
                      f"{pick.book}", f"news:{gid}:spreads:{pick.side}:{change_key}",
                      None, pick.book_prob, None, event_id, study,
                      {"margin_before": rp.margin_before,
                       "margin_after": rp.margin_after})  # fmt: skip
            out.append(emit(self.pdb, a, now, self.keys.ntfy_topic, self.push_client))
        dk = _props(qs, "draftkings")
        affected = {name for name, c in players.items()}
        for x in rp.props:
            changed = players.get(x["player_name"])
            mates = any(c.team_id == x["team_id"] for c in players.values())
            self_ok = changed is not None and changed.new_status in ("Questionable", "Available")
            if not mates or (x["player_name"] in affected and not self_ok):
                continue
            line = dk.get((_norm(x["player_name"]), x["market"]))
            if line is None:
                continue
            p_over = p_over_fn(x["market"], x["mean_after"], line["line"])
            pick = R.prop_signal(x["mean_before"], x["mean_after"], p_over, line["line"],
                                 line.get("over"), line.get("under"), "draftkings")
            if pick is None:
                continue
            a = Alert("news", sport, gid, label, tip, x["market"], pick.side, pick.line,
                      pick.price, "draftkings", f"{reason}; projection {x['mean_before']:.1f} -> "
                      f"{x['mean_after']:.1f}, model {100 * pick.fair_prob:.0f}% vs no-vig "
                      f"{100 * pick.book_prob:.0f}%",
                      f"news:{gid}:{x['market']}:{_norm(x['player_name'])}:{pick.side}:"
                      f"{change_key}", pick.fair_prob, pick.book_prob, x["player_name"],
                      event_id, study, {"mean_before": x["mean_before"],
                                        "mean_after": x["mean_after"]})  # fmt: skip
            out.append(emit(self.pdb, a, now, self.keys.ntfy_topic, self.push_client))
        return [x for x in out if x is not None]

    # --- polling prices -----------------------------------------------------------------------

    def store_quotes(self, qs: list[Quote], now: datetime, event_id: int | None = None) -> None:
        if not qs:
            return
        with self.pdb.begin() as p:
            p.execute(insert(quotes), [dict(
                polled_at=now, sport=q.sport, game_id=q.game_id, event_id=event_id,
                source=q.source, book=q.book, market=q.market, player_name=q.player_name,
                outcome=q.outcome, line=q.line, price=q.price,
                book_updated_at=q.book_updated_at) for q in qs])  # fmt: skip

    def poll_game(self, sport: str, gid: int, now: datetime, event_id: int | None) -> list[Quote]:
        """DK/FD main lines and Signal 1 props for one game, plus Kalshi; stored."""
        qs: list[Quote] = []
        if self.sharp is not None:
            with self.main.connect() as c:
                for ev in self.sharp.event_ids(c, gid):
                    qs += [q for q in self.sharp.game(c, sport, ev) if q.game_id == gid]
                if not qs:  # no stored SharpAPI id yet: read the board and match by teams
                    qs = [q for q in self.sharp.board(c, sport, now + timedelta(days=2))
                          if q.game_id == gid]  # fmt: skip
        with self.main.connect() as c:
            qs += [q for q in self._guard("kalshi", self.kalshi.winners, c, sport) or []
                   if q.game_id == gid]  # fmt: skip
        self.store_quotes(qs, now, event_id)
        return qs

    def open_lag(self, sport: str, gid: int, event_id: int, now: datetime) -> None:
        with self.pdb.begin() as p:
            windows = get_state(p, "lag:open", {})
            key = f"{sport}:{gid}"
            until = (now + timedelta(minutes=R.LAG_WINDOW_MIN)).isoformat()
            windows[key] = {"until": max(until, windows.get(key, {}).get("until", until)),
                            "event_id": windows.get(key, {}).get("event_id", event_id)}
            set_state(p, "lag:open", windows, now)

    def lag_polls(self, now: datetime) -> None:
        with self.pdb.begin() as p:
            windows = get_state(p, "lag:open", {})
        keep = {}
        for key, w in windows.items():
            if datetime.fromisoformat(w["until"]) < now:
                continue
            keep[key] = w
            sport, gid = key.split(":")
            cadence = NBA_CADENCE if sport == "nba" else NFL_CADENCE
            if self._due(f"due:lag:{key}", cadence, now):
                self.poll_game(sport, int(gid), now, w.get("event_id"))
        if keep != windows:
            with self.pdb.begin() as p:
                set_state(p, "lag:open", keep, now)

    # --- Signal 2 -----------------------------------------------------------------------------

    def board_times(self, now: datetime, exact: bool = True) -> list[tuple[str, str]]:
        """(sport, slot key) of the Signal 2 boards due now (this minute; with exact=False,
        within the next 5 minutes)."""
        if self.pin is None or self.sharp is None:
            return []
        local = now.astimezone(R.EASTERN)
        span = timedelta(minutes=1 if exact else 5)
        out = []
        with self.main.connect() as c:
            for hh, mm in NBA_BOARDS_ET:
                slot = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if timedelta(0) <= local - slot < span and self._tips_within(
                        c, "nba", now, timedelta(hours=3)):  # fmt: skip
                    out.append(("nba", f"{local:%Y-%m-%d}T{hh:02d}{mm:02d}"))
            for tip in self._kickoffs(c, "nfl", now):
                if timedelta(0) <= now - (tip - NFL_BOARD_BEFORE) < span:
                    out.append(("nfl", tip.isoformat()))
        return out

    def _tips_within(self, c, sport, now, span) -> bool:
        return bool(self._kickoffs(c, sport, now, span))

    def _kickoffs(self, c, sport, now, span=timedelta(hours=3)) -> list[datetime]:
        from betdata.core.matching import sport_id

        tips = c.execute(select(games.c.start_time_utc).where(
            games.c.sport_id == sport_id(c, sport),
            games.c.status.notin_(("postponed", "cancelled")),
            games.c.start_time_utc > now, games.c.start_time_utc <= now + span)).scalars()
        return sorted({utc(t) for t in tips})

    def sharp_boards(self, now: datetime) -> None:
        if self.pin is None or self.sharp is None:
            return
        for sport, slot in self.board_times(now):
            if not self._due(f"due:board:{sport}:{slot}", timedelta(days=1), now):
                continue
            with self.main.connect() as c:
                pin = self.pin.board(c, sport, now)
                if pin is None:
                    continue
                soft = self.sharp.board(c, sport, now + timedelta(hours=6))
            self.store_quotes(pin + soft, now)
            self.signal_sharp(sport, pin, soft, now)

    def signal_sharp(self, sport: str, pin: list[Quote], soft: list[Quote],
                     now: datetime) -> list[int]:  # fmt: skip
        out = []
        pins = _two_way(pin, now)
        softs = _two_way(soft, now)
        for (gid, market), pq in pins.items():
            sides = ("over", "under") if market == "totals" else ("home", "away")
            best: dict[str, R.Pick] = {}
            for book in ("draftkings", "fanduel"):
                sq = softs.get((gid, market), {}).get(book)
                pq1 = pq.get("pinnacle")
                if sq is None or pq1 is None:
                    continue
                for pick in R.sharp_signal(pq1, sq, sides):
                    if pick.side not in best or (pick.edge, R.decimal(pick.price)) > (
                            best[pick.side].edge, R.decimal(best[pick.side].price)):  # fmt: skip
                        best[pick.side] = pick
            for side in sides:
                key = f"s2:{gid}:{market}:{side}"
                with self.pdb.begin() as p:
                    st = get_state(p, key, {"open": False, "n": 0})
                    pick = best.get(side)
                    if pick is None:
                        if st["open"]:
                            set_state(p, key, {"open": False, "n": st["n"]}, now)
                        continue
                    if st["open"]:
                        continue
                    set_state(p, key, {"open": True, "n": st["n"] + 1}, now)
                label, tip = self.game_label(gid)
                a = Alert("sharp", sport, gid, label, tip, market, pick.side, pick.line,
                          pick.price, pick.book,
                          f"Pinnacle no-vig {100 * pick.fair_prob:.1f}% vs {pick.book} "
                          f"{100 * pick.book_prob:.1f}% (gap {100 * pick.edge:.1f} pp); EV "
                          f"{100 * (pick.fair_prob * R.decimal(pick.price) - 1):+.1f}%",
                          f"sharp:{gid}:{market}:{side}:{st['n'] + 1}", pick.fair_prob,
                          pick.book_prob)  # fmt: skip
                aid = emit(self.pdb, a, now, self.keys.ntfy_topic, self.push_client)
                if aid is not None:
                    out.append(aid)
        return out

    # --- closing polls and grading ----------------------------------------------------------------

    def closing_polls(self, now: datetime) -> None:
        if self.sharp is None:
            return
        with self.pdb.begin() as p:
            rows = p.execute(select(alerts.c.sport, alerts.c.game_id, alerts.c.tip).where(
                alerts.c.tip > now,
                alerts.c.tip <= now + timedelta(minutes=CLOSING_MIN))).all()  # fmt: skip
        for sport, gid in sorted({(r.sport, r.game_id) for r in rows}):
            if self._due(f"due:close:{sport}:{gid}", timedelta(minutes=4), now):
                self.poll_game(sport, gid, now, None)

    def grade(self, now: datetime) -> None:
        """At the start of each job: grade alerts, and take the Signal 1 decision once the
        lag study's two weeks are up."""
        from betdata.alerts import grade as G
        from betdata.alerts import lag as L

        self._guard("grade", G.grade_all, self.main, self.pdb, now)

        def _decide():
            with self.pdb.begin() as p, self.main.connect() as m:
                return L.decide(p, m, now)

        self._guard("lag decision", _decide)

    # --- helpers --------------------------------------------------------------------------------

    def game_label(self, gid: int) -> tuple[str, datetime]:
        h, a = teams.alias("h"), teams.alias("a")
        with self.main.connect() as c:
            r = c.execute(select(h.c.abbrev.label("home"), a.c.abbrev.label("away"),
                                 games.c.start_time_utc)
                          .join(h, h.c.team_id == games.c.home_team_id)
                          .join(a, a.c.team_id == games.c.away_team_id)
                          .where(games.c.game_id == gid)).one()  # fmt: skip
        return f"{r.away} @ {r.home}", utc(r.start_time_utc)


def _norm(name: str) -> str:
    import re
    import unicodedata

    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    if "," in s:  # "Embiid, Joel"
        last, first = s.split(",", 1)
        s = f"{first} {last}"
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", s)
    return re.sub(r"[^a-z]", "", s)


def _spreads(qs: list[Quote]) -> list[R.Spread]:
    out = []
    for book in ("draftkings", "fanduel"):
        h = next((q for q in qs if q.book == book and q.market == "spreads"
                  and q.outcome == "home" and not q.player_name), None)  # fmt: skip
        a = next((q for q in qs if q.book == book and q.market == "spreads"
                  and q.outcome == "away" and not q.player_name), None)  # fmt: skip
        if h and a and h.line is not None and h.price and a.price:
            out.append(R.Spread(book, h.line, h.price, a.price))
    return out


def _props(qs: list[Quote], book: str) -> dict:
    """(normalized player, market) -> {line, over, under} at one book (one line per player)."""
    out: dict = {}
    for q in qs:
        if q.book != book or not q.player_name or q.line is None:
            continue
        d = out.setdefault((_norm(q.player_name), q.market), {"line": q.line})
        if d["line"] == q.line:
            d[q.outcome] = q.price
    return out


def _two_way(qs: list[Quote], now: datetime) -> dict:
    """(game, market) -> {book: TwoWay}, home/over first; spreads keyed by the home line."""
    pairs: dict = {}
    for q in qs:
        if q.player_name or q.price is None:
            continue
        pairs.setdefault((q.game_id, q.market, q.book), {})[q.outcome] = q
    out: dict = {}
    for (gid, market, book), d in pairs.items():
        a, b = (d.get("over"), d.get("under")) if market == "totals" else (d.get("home"),
                                                                            d.get("away"))
        if a is None or b is None:
            continue
        line = a.line if market != "h2h" else None
        out.setdefault((gid, market), {})[book] = R.TwoWay(book, line, a.price, b.price, 0.0)
    return out


__all__ = ["Keys", "Poller", "hourly_running"]
