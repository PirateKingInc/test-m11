"""NBA in the M16 poller: read the newest official injury report, find qualifying status
changes, and re-predict only the affected game with the frozen page models.

All of it runs on the poller's throwaway copy of the hourly database: a new report is loaded
there (the same change-only loader the hourly job uses), the as-of store is reloaded, and each
rotation player's play probability is computed by the M14 model's own rules just before and
just after the report became known. Nothing here writes the hourly state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

import numpy as np
from sqlalchemy import Engine, func, select

from betdata.alerts.rules import EASTERN, qualifies_nba
from betdata.db.schema import games, nba_injury_reports
from betdata.nba.features import play_prob, status_key, streak_for, team_status
from betdata.nba.injury_pdf import parse_lines, pdf_lines
from betdata.nba.live import loaded_store, point_now
from betdata.nba.state import League
from betdata.nfl.features.state import advanced
from betdata.nfl.store import AsOf
from betdata.sources.nba_injury import BASE, latest_names, load_report

log = logging.getLogger(__name__)
CADENCE = timedelta(minutes=3)
GAME_DAY = (time(10, 0), time(19, 30))  # Eastern, on game days
DAY_BEFORE = (time(16, 30), time(20, 30))  # the 5 pm local deadline, Eastern to Pacific


@dataclass
class Change:
    game_id: int
    tip: datetime
    player_key: str
    player_name: str
    team_id: int
    old_status: str | None
    new_status: str | None
    prob_before: float
    prob_after: float
    min_play: float
    qualifies: bool
    report_name: str
    report_at: datetime
    known_at: datetime


@dataclass
class Report:
    name: str
    report_at: datetime  # stated (Eastern, aware)
    known_at: datetime  # when the rows became known in the database
    changes: list[Change] = field(default_factory=list)


def active(conn, now: datetime) -> bool:
    """Report windows: game days 10:00-19:30 ET, and 16:30-20:30 ET the day before a game day."""
    local = now.astimezone(EASTERN)
    days = _game_days(conn, local.date(), local.date() + timedelta(days=1))
    t = local.time()
    return (local.date() in days and GAME_DAY[0] <= t < GAME_DAY[1]) or (
        local.date() + timedelta(days=1) in days and DAY_BEFORE[0] <= t < DAY_BEFORE[1])


def _game_days(conn, lo, hi) -> set:
    from betdata.core.matching import sport_id

    start = datetime.combine(lo, time(0), EASTERN)
    end = datetime.combine(hi + timedelta(days=1), time(6), EASTERN)
    tips = conn.execute(select(games.c.start_time_utc).where(
        games.c.sport_id == sport_id(conn, "nba"),
        games.c.status.notin_(("postponed", "cancelled")),
        games.c.start_time_utc >= start, games.c.start_time_utc < end)).scalars()  # fmt: skip
    return {_utc(t).astimezone(EASTERN).date() for t in tips}


def _utc(t: datetime) -> datetime:
    from datetime import UTC

    return t if t.tzinfo else t.replace(tzinfo=UTC)


def newest_report(http, now: datetime, last_name: str | None) -> tuple[str, bytes] | None:
    """The newest published report newer than `last_name` (names are newest first), or None."""
    for name in latest_names(now):
        if name == last_name:
            return None
        r = http.get("nba_injury", f"{BASE}/{name}")
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            return name, r.content
    return None


def load_new_report(engine: Engine, name: str, pdf: bytes, now: datetime) -> Report:
    """Load one report into the throwaway copy (change-only, as the hourly job does) and return
    its stated time and when its rows became known (ours, or the hourly job's if it read the
    same report first)."""
    rep = parse_lines(pdf_lines(pdf))
    stated = rep.report_at.replace(tzinfo=EASTERN)
    payload = {"name": name, "report_at": stated, "known_at": now, "rows": rep.rows,
               "teams": [[*k, v] for k, v in rep.teams.items()]}  # fmt: skip
    with engine.begin() as conn:
        load_report(conn, payload, fetched_at=now, raw_id=None)
        known = conn.execute(select(func.min(nba_injury_reports.c.known_at)).where(
            nba_injury_reports.c.report_at == stated)).scalar()  # fmt: skip
    return Report(name, stated, _utc(known) if known else now)


def detect(engine: Engine, store, report: Report, now: datetime) -> list[Change]:
    """Every rotation player of a game on the report (tip within 36 hours) whose play
    probability differs just before vs just after the report became known."""
    g = store.games
    soon = [gid for gid in g.index if g.at[gid, "status"] not in ("postponed", "cancelled")
            and now < _utc(g.at[gid, "tip"]) <= now + timedelta(hours=36)]  # fmt: skip
    before, after = AsOf(store, report.known_at - timedelta(seconds=1)), AsOf(store,
                                                                              report.known_at)
    out = []
    for gid in soon:
        game = g.loc[gid]
        for team in (game.home, game.away):
            pb, sb = _probs(before, game, team)
            pa, sa = _probs(after, game, team)
            for pid, (prob_a, min_play) in pa.items():
                prob_b = pb.get(pid, (prob_a, min_play))[0]
                if abs(prob_a - prob_b) < 1e-9:
                    continue
                out.append(Change(int(gid), _utc(game.tip), str(pid),
                                  store.names.get(pid, str(pid)), int(team), sb.get(pid),
                                  sa.get(pid), round(prob_b, 4), round(prob_a, 4),
                                  round(min_play, 1), qualifies_nba(min_play, prob_b, prob_a),
                                  report.name, report.report_at, report.known_at))  # fmt: skip
    return out


def _probs(a: AsOf, game, team) -> tuple[dict, dict]:
    st = advanced(a, "league", League)
    status, reported = team_status(a, game, team)
    out = {}
    for pid, p in st.rotation(team):
        streak = streak_for(st, game, p)
        out[pid] = (play_prob(a, status_key(status.get(pid), streak, reported)), p.min_play)
    return out, status


@dataclass
class Repredicted:
    model_version: str
    point: str
    margin_before: float
    margin_after: float
    total_before: float
    total_after: float
    props: list[dict]  # {player_name, team_id, market, mean_before, mean_after, p_over_fn}


class Models:
    """The frozen page models, loaded once per poller job."""

    def __init__(self, engine: Engine, now: datetime):
        from betdata.nba.live15 import Design15, load_fit15, rapm_snapshot
        from betdata.nba.props.live import PropDesign
        from betdata.nba.props.live import load_model as load_props

        self.design = Design15.load()
        self.fits, self.play, self.load_sit, self.version = load_fit15(self.design)
        self.rapm = rapm_snapshot(engine, now)
        self.prop_design = PropDesign.load()
        self.prop_model, self.prop_version = load_props(self.prop_design)

    def store(self, engine: Engine, now: datetime):
        return loaded_store(engine, now, self.play).with_params(load_sit=self.load_sit,
                                                               rapm=self.rapm)  # fmt: skip

    def repredict(self, engine: Engine, store, change: Change, now: datetime) -> Repredicted:
        from betdata.nba.live15 import predict_game
        from betdata.nba.props.live import predict as predict_props

        g = store.games.loc[change.game_id]
        point = point_now(_utc(g.tip), now)
        t0, t1 = change.known_at - timedelta(seconds=1), max(change.known_at, now)
        b = predict_game(store, self.design, self.fits, g, point, t0)
        a = predict_game(store, self.design, self.fits, g, point, t1)
        props = []
        with engine.connect() as conn:
            from betdata.core.matching import season_for
            from betdata.nba.props.panel import Panel

            panel = Panel.load(conn, season_for("nba", now) - 1)
            before = {(p["player_id"], p["market"]): p for p in predict_props(
                conn, t0, panel, self.prop_design) if p["game_id"] == change.game_id}
            for p in predict_props(conn, t1, panel, self.prop_design):
                key = (p["player_id"], p["market"])
                if p["game_id"] != change.game_id or key not in before:
                    continue
                props.append(dict(player_name=p["player_name"], player_id=p["player_id"],
                                  team_id=p["team_id"], market=p["market"],
                                  mean_before=before[key]["mean"], mean_after=p["mean"]))
        return Repredicted(self.version, point, b.margin, a.margin, b.total, a.total, props)

    def p_over(self, market: str, mean: float, line: float) -> float:
        from betdata.nba.props.live import p_over_no_push

        return float(np.asarray(p_over_no_push(self.prop_model, market, [mean], [line]))[0])
