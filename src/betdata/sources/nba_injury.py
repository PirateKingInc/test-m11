"""The official NBA injury report (ak-static.cms.nba.com/referee/injury/), every version kept.

  report    the latest published report (hourly run)
  history   the reports of past game days (--start 2019-01-01 --end 2019-01-31)

File names by era (MODEL.md): `Injury-Report_2019-01-10_05PM.pdf` (three a day 2018-21, hourly
2021-26; the stated time is half past the hour in the name) and `..._05_15PM.pdf` (every 15
minutes since early 2026; the stated time is the name). History loads, per game day, the
hourly reports 11 AM-10 PM ET (three a day in the early era); the 15-minute era loads the
reports on the hour and at :45, which is what the two prediction points read.

Each PDF is reduced to its rows before archiving (`nba.injury_pdf`), so the archive keeps the
parsed table, not the PDF. Rows are stored change-only per game and player
(`nba_injury_reports`, see the schema). The server refuses bursts: requests are spaced by
`min_interval_seconds`.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, and_, func, insert, select

from betdata.config import SourceConfig
from betdata.core import raw_store
from betdata.core.errors import ApiError, ConfigError, NothingToDo
from betdata.core.http import SourceClient
from betdata.core.matching import sport_id
from betdata.core.normalize.teams import TeamResolver
from betdata.core.source import JobSpec, RawInput, Source
from betdata.db.schema import games, nba_injury_reports, teams
from betdata.nba.injury_pdf import parse_lines, pdf_lines

NAME = "nba_injury"
log = logging.getLogger(__name__)
EASTERN = ZoneInfo("America/New_York")
BASE = "https://ak-static.cms.nba.com/referee/injury"
POSTING_MARGIN = timedelta(minutes=15)  # history: a report is readable 15 min after its time
QUARTER_ERA = date(2025, 12, 1)  # on or after this, look for 15-minute file names first
HOURLY_ERA = date(2021, 7, 1)
EARLY_HOURS = (11, 13, 14, 17, 20)  # three-a-day era (bubble: 11 AM, 2 PM, 5 PM)
HISTORY_HOURS = tuple(range(11, 23))


def file_name(day: date, hour: int, minute: int | None = None) -> str:
    h12 = hour % 12 or 12
    ampm = "AM" if hour < 12 else "PM"
    if minute is None:
        return f"Injury-Report_{day:%Y-%m-%d}_{h12:02d}{ampm}.pdf"
    return f"Injury-Report_{day:%Y-%m-%d}_{h12:02d}_{minute:02d}{ampm}.pdf"


def history_names(day: date) -> list[str]:
    if day >= QUARTER_ERA:
        names = [file_name(day, h, m) for h in HISTORY_HOURS for m in (0, 45)]
        return names + [file_name(day, h) for h in HISTORY_HOURS]  # the hourly era's tail
    if day >= HOURLY_ERA:
        return [file_name(day, h) for h in HISTORY_HOURS]
    return [file_name(day, h) for h in EARLY_HOURS]


def latest_names(now: datetime) -> list[str]:
    """Newest first: the last eight quarter-hours, then the last three hourly names."""
    local = now.astimezone(EASTERN)
    q = local.replace(minute=local.minute - local.minute % 15, second=0, microsecond=0)
    out = [file_name(t.date(), t.hour, t.minute) for t in (q - timedelta(minutes=15 * i)
                                                          for i in range(8))]  # fmt: skip
    hour = local.replace(minute=0, second=0, microsecond=0)
    return out + [file_name(t.date(), t.hour) for t in (hour - timedelta(hours=i)
                                                       for i in range(3))]  # fmt: skip


def parse_payload(body: bytes) -> list[dict]:
    """The archived body: a list of reports, each {name, report_at (ET), known_at, rows,
    teams}."""
    reports = json.loads(body)["reports"]
    for r in reports:
        r["report_at"] = datetime.fromisoformat(r["report_at"])
        r["known_at"] = datetime.fromisoformat(r["known_at"])
    return reports


class NbaInjurySource(Source):
    name = NAME

    def __init__(self, engine, config: SourceConfig, *, transport=None, sleep=None, clock=None):
        super().__init__(engine, config, clock)
        extra = {k: v for k, v in dict(sleep=sleep, clock=clock).items() if v is not None}
        self.client = SourceClient(engine, NAME, config.options.get("base_url", BASE),
                                   transport=transport, max_attempts=2,
                                   min_interval=config.options.get("min_interval_seconds", 1.0),
                                   breaker_threshold=10_000, **extra)  # fmt: skip

    @classmethod
    def jobs(cls) -> dict[str, JobSpec]:
        return {"report": JobSpec("report", "The latest official injury report"),
                "history": JobSpec("history", "Past reports (--start 2019-01-01 --end "
                                   "2019-01-31; game days only)")}  # fmt: skip

    def _pdf(self, name: str, run_id: int) -> bytes | None:
        try:
            return self.client.get(f"/{name}", est_cost=0, run_id=run_id, store_raw=False).body
        except ApiError as e:
            if e.status in (403, 404):  # not published (the server answers 403 for those)
                return None
            raise

    def _report(self, name: str, pdf: bytes, live_at: datetime | None) -> dict:
        rep = parse_lines(pdf_lines(pdf))
        stated = rep.report_at.replace(tzinfo=EASTERN)
        known = live_at if live_at is not None else stated + POSTING_MARGIN
        return {"name": name, "report_at": stated.isoformat(), "known_at": known.isoformat(),
                "rows": rep.rows,
                "teams": [[*k, v] for k, v in rep.teams.items()]}  # fmt: skip

    def fetch(self, job: str, params: dict[str, Any], *, priority: str, run_id: int) -> RawInput:
        reports = []
        if job == "report":
            for name in latest_names(self.clock()):
                pdf = self._pdf(name, run_id)
                if pdf is not None:
                    reports.append(self._report(name, pdf, self.clock()))
                    break
            if not reports:
                raise NothingToDo("no injury report published in the last two hours")
        elif job == "history":
            try:
                start = date.fromisoformat(str(params["start"]))
                end = date.fromisoformat(str(params.get("end", params["start"])))
            except (KeyError, ValueError) as e:
                raise ConfigError("history needs --start YYYY-MM-DD [--end YYYY-MM-DD]") from e
            for day in self._game_days(start, end):
                found_quarter = False
                for name in history_names(day):
                    hourly = name.count("_") == 2
                    if hourly and found_quarter:
                        continue  # this day is in the 15-minute era
                    pdf = self._pdf(name, run_id)
                    if pdf is not None:
                        found_quarter |= not hourly
                        reports.append(self._report(name, pdf, None))
        else:
            raise KeyError(job)
        body = json.dumps({"reports": reports}, default=str).encode()
        at = self.clock()
        with self.engine.begin() as conn:
            raw_id = raw_store.save(conn, source=NAME, endpoint=f"/{job}", params=params or None,
                                    http_status=200, fetched_at=at, body=body,
                                    content_type="application/json")  # fmt: skip
        return RawInput(f"/{job}", params, body, at, raw_id)

    def _game_days(self, start: date, end: date) -> list[date]:
        """Eastern dates with NBA games in [start, end] (from the stored schedule)."""
        with self.engine.connect() as conn:
            tips = conn.execute(
                select(games.c.start_time_utc).where(
                    games.c.sport_id == sport_id(conn, "nba"),
                    games.c.start_time_utc >= datetime.combine(start, time(0), EASTERN),
                    games.c.start_time_utc < datetime.combine(end + timedelta(days=1), time(6),
                                                              EASTERN))).scalars()  # fmt: skip
            days = {t.astimezone(EASTERN).date() for t in tips}
        return sorted(d for d in days if start <= d <= end)

    def job_for_endpoint(self, endpoint: str) -> str:
        return endpoint.strip("/")

    def parse(self, job: str, raw: RawInput) -> list[dict]:
        return parse_payload(raw.body)

    def load(self, conn, job: str, batch, *, run_id, raw_id) -> tuple[int, int]:
        n = 0
        for rep in sorted(batch, key=lambda r: r["report_at"]):
            n += load_report(conn, rep, fetched_at=self.clock(), raw_id=raw_id)
        return n, sum(len(r["rows"]) for r in batch)


def _games_index(conn: Connection, dates: set[str]) -> dict[tuple[str, str], int]:
    """(Eastern date, AWAY@HOME) -> game_id."""
    sid = sport_id(conn, "nba")
    h, a = teams.alias("h"), teams.alias("a")
    lo = min(date.fromisoformat(d) for d in dates)
    hi = max(date.fromisoformat(d) for d in dates)
    rows = conn.execute(
        select(games.c.game_id, games.c.start_time_utc, h.c.abbrev.label("home"),
               a.c.abbrev.label("away"))
        .join(h, h.c.team_id == games.c.home_team_id).join(a, a.c.team_id == games.c.away_team_id)
        .where(games.c.sport_id == sid,
               games.c.start_time_utc >= datetime.combine(lo, time(0), EASTERN),
               games.c.start_time_utc < datetime.combine(hi + timedelta(days=1), time(6),
                                                         EASTERN))).all()  # fmt: skip
    return {(r.start_time_utc.astimezone(EASTERN).date().isoformat(), f"{r.away}@{r.home}"):
            r.game_id for r in rows}  # fmt: skip


def load_report(conn: Connection, rep: dict, *, fetched_at: datetime,
                raw_id: int | None) -> int:  # fmt: skip
    """Change-only insert of one report's rows, plus `Removed` rows for players who were listed
    before for a game this report still covers (and whose team submitted)."""
    t = nba_injury_reports
    stated = rep["report_at"]
    games_ix = _games_index(conn, {r[0] for r in rep["teams"]} | {r["game_date"]
                                                                  for r in rep["rows"]})
    resolver = TeamResolver(conn, sport_id(conn, "nba"))
    covered = {(d, m, team) for d, m, team, submitted in rep["teams"] if submitted}
    keys = {(r["game_date"], r["matchup"]) for r in rep["rows"]} | {(d, m) for d, m, *_ in
                                                                    rep["teams"]}
    latest: dict[tuple[str, str, str], Any] = {}
    for d, m in keys:
        prior = (select(t.c.player_name, t.c.team_name, t.c.status, t.c.reason,
                        func.row_number().over(partition_by=t.c.player_name,
                                               order_by=t.c.report_at.desc()).label("rn"))
                 .where(t.c.game_date == d, t.c.matchup == m, t.c.report_at < stated)
                 .subquery())  # fmt: skip
        for p in conn.execute(select(prior).where(prior.c.rn == 1)):
            latest[(d, m, p.player_name)] = p
    new = []
    listed = set()
    for r in rep["rows"]:
        key = (r["game_date"], r["matchup"], r["player_name"])
        listed.add(key)
        prev = latest.get(key)
        if prev is not None and (prev.status, prev.reason) == (r["status"], r["reason"]):
            continue
        new.append(dict(r))
    for (d, m, player), prev in latest.items():
        if (d, m, prev.team_name) in covered and (d, m, player) not in listed \
                and prev.status != "Removed":
            new.append(dict(game_date=d, matchup=m, team_name=prev.team_name,
                            player_name=player, status="Removed", reason=None))  # fmt: skip
    if not new:
        return 0
    exists_at = set(conn.execute(select(t.c.game_date, t.c.matchup, t.c.player_name).where(
        t.c.report_at == stated, and_(t.c.game_date.in_({n["game_date"] for n in new})))).all())
    rows = []
    for n in new:
        if (n["game_date"], n["matchup"], n["player_name"]) in exists_at:
            continue  # the same report loaded twice
        rows.append(dict(
            game_date=n["game_date"], matchup=n["matchup"],
            game_id=games_ix.get((n["game_date"], n["matchup"])),
            team_id=resolver.lookup(NAME, n["team_name"]), team_name=n["team_name"][:64],
            player_name=n["player_name"][:128], status=n["status"], reason=n.get("reason"),
            report_at=stated, known_at=rep["known_at"], source=NAME, fetched_at=fetched_at,
            raw_id=raw_id))  # fmt: skip
    if rows:
        conn.execute(insert(t), rows)
    return len(rows)
