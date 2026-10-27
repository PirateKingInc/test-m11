"""NFL in the M16 poller: ESPN injury statuses and game-day inactives in the hour before each
kickoff window, the MODEL.md value test, and a re-prediction of only the affected game with the
frozen page models (side m13w, props nflp15).

Sources (free, no key; shapes verified live from GitHub Actions on 2026-10-22):
- the ESPN game summary (`site.api.espn.com/.../nfl/summary?event=<id>`), key `injuries`: one
  block per team `{team: {id, abbreviation}, injuries: [{status, date, athlete: {id,
  displayName, position?}}]}`;
- the ESPN core roster of each competitor (`sports.core.api.espn.com/.../competitors/<team>/
  roster`), `entries: [{playerId, active, starter, didNotPlay, displayName}]`. Game-day
  inactives are released about 90 minutes before kickoff; before that `active` is unreliable,
  so a player counts as inactive only when the entries show `active=false` AND at least
  `MIN_ACTIVES` (40) entries are active, i.e. the inactive list is out. Rosters are read from
  95 minutes before kickoff on (earlier reads would only spend requests).

Rules (MODEL.md "Status changes that qualify", NFL), and the simplifications they need:
- **Window:** every 5 minutes from 120 to 60 minutes before each kickoff window (games with the
  same kickoff); only games in an active window are read.
- **Changes:** the last seen status per (game, ESPN athlete id) is kept in the poller database
  (`nfl:status:<game>`), so a change is found across cycles and across jobs. The first read of
  a game is a baseline: nothing is recorded unless a player's ESPN status gives a different M12
  play probability from what M12 already knew (the latest nflverse injury-report row of that
  week, else the weekly roster's reserve status, else not listed), and then the change is
  recorded with M12's status as the old one. A player who drops off the ESPN list counts as a
  change to "not listed" only when his team's list is not empty (an empty list is taken as a
  feed gap). An inactive player (roster released) is "Inactive"; a Questionable/Doubtful
  player who is active once the list is released is "Active".
- **Play probability:** M12's own table (`play_prob` of `nfl/features/availability.py`, the
  training-season fit stored with the frozen prop fit, or the hourly job's cached params).
  Buckets: a report status with the player's practice status from M12's report rows of the week
  (`fr|<status>|<practice>`; with no practice row, DNP for Out/Doubtful and LP for Questionable,
  since M12's table has no bucket without one), Inactive = `pk_out`, Active =
  `pk_act|<his report status>`, reserve lists (IR, PUP, NFI, suspension) = `fr_reserve`, not
  listed = `fr|-|-`.
- **Snap share:** his share of his unit's snaps (offense for QB/RB/WR/TE/OL, else defense) over
  his last 4 games played (`player_game_snaps`, this and last season), found by name: rows of
  his current team first, else a name that maps to one player only. OL and defensive players
  are passed as starters (the 80% share is the test).
- **Re-prediction:** ESPN statuses are written into the as-of store as of one second before
  now (injury-report rows; an inactive becomes a roster INA row when the game-day roster is
  already in the store, else an Out report row), and the game is predicted at now with the
  statuses known before this change vs with the new ones. Same instant, same point: only the
  news differs. The m13w fit is read from its cache (`data/derived`, saved by the hourly job);
  it is never refitted here. If it is missing, the change is still recorded and its lag window
  opened, but no Signal 1 alert is possible (the error is recorded).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import Connection, Engine, insert, select

from betdata.alerts.db import get_state, repredictions, set_state
from betdata.alerts.rules import EASTERN, qualifies_nfl
from betdata.analysis.context import POSITION_GROUP
from betdata.db.schema import games, injuries, player_game_snaps, roster_weeks, teams
from betdata.nfl.features.availability import PRACTICE, play_prob
from betdata.nfl.props.ids import norm_name

log = logging.getLogger(__name__)

CADENCE = timedelta(minutes=5)
WINDOW_FROM = timedelta(minutes=120)  # window opens this long before kickoff
WINDOW_TO = timedelta(minutes=60)  # and closes this long before
ROSTER_FROM = timedelta(minutes=95)  # inactives come out ~90 minutes before kickoff
MIN_ACTIVES = 40  # active entries that show the inactive list was released
SNAP_GAMES = 4

SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ROSTER = ("https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/{e}/"
          "competitions/{e}/competitors/{t}/roster")  # fmt: skip
ESPN_ABBR = {"WSH": "WAS", "LAR": "LA", "JAC": "JAX", "LVR": "LV"}

GAME_STATUSES = ("Out", "Doubtful", "Questionable")
DEFAULT_PRACTICE = {"Out": "Did Not Participate In Practice",
                    "Doubtful": "Did Not Participate In Practice",
                    "Questionable": "Limited Participation in Practice"}  # fmt: skip
RESERVE_WORDS = ("reserve", "pup", "physically unable", "non-football", "suspen", "nfi")
OFFENSE = {"QB", "OL", "WR", "TE", "RB"}
STARTER_GROUPS = {"OL", "DL", "LB", "CB", "S"}
INACTIVE, ACTIVE = "Inactive", "Active"


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _when(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return _utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def is_reserve(status: str | None) -> bool:
    s = (status or "").lower()
    return any(w in s for w in RESERVE_WORDS)


def bucket(status: str | None, practice: str | None = None, report: str | None = None) -> str:
    """M12's play-probability bucket for a status (module docstring)."""
    if status is None:
        return "fr|-|-"
    if status == INACTIVE:
        return "pk_out"
    if status == ACTIVE:
        return f"pk_act|{report if report in GAME_STATUSES else '-'}"
    if is_reserve(status):
        return "fr_reserve"
    if status in GAME_STATUSES:
        ps = PRACTICE.get(practice or "") or PRACTICE[DEFAULT_PRACTICE[status]]
        return f"fr|{status}|{ps}"
    return "fr|-|-"


def prob(params: dict, status: str | None, practice: str | None = None,
         report: str | None = None) -> float:  # fmt: skip
    return float(play_prob(params, bucket(status, practice, report)))


@dataclass
class Change:
    game_id: int
    tip: datetime
    player_key: str  # ESPN athlete id
    player_name: str
    team_id: int | None
    old_status: str | None
    new_status: str | None
    prob_before: float
    prob_after: float
    snap_share: float | None
    position: str | None
    qualifies: bool
    report_name: str
    report_at: datetime
    baseline: bool
    gsis: str | None


class NflHandler:
    """Called every poller cycle (`Poller.cycle`); does nothing outside the NFL windows."""

    def __init__(self, models_factory=None, params: dict | None = None):
        self._models_factory = models_factory or Models
        self._models = None
        self._params = params
        self._snaps: dict = {}

    # --- windows ---------------------------------------------------------------------------

    def _kickoffs(self, conn: Connection, t: datetime) -> list:
        from betdata.core.matching import sport_id

        return conn.execute(select(
            games.c.game_id, games.c.season, games.c.week, games.c.start_time_utc,
            games.c.home_team_id, games.c.away_team_id).where(
            games.c.sport_id == sport_id(conn, "nfl"),
            games.c.status.notin_(("postponed", "cancelled", "final")),
            games.c.start_time_utc > t + WINDOW_TO,
            games.c.start_time_utc <= t + WINDOW_FROM)).all()  # fmt: skip

    def window_games(self, conn: Connection, t: datetime) -> list:
        """Games whose kickoff window is open at t (120 to 60 minutes before kickoff)."""
        return sorted(self._kickoffs(conn, t), key=lambda r: (_utc(r.start_time_utc), r.game_id))

    def active(self, conn: Connection, t: datetime) -> bool:
        return bool(self._kickoffs(conn, t))

    def cycle(self, poller, now: datetime) -> int:
        with poller.main.connect() as c:
            if not self.active(c, now):
                return 0
        if not poller._due("due:nfl_status", CADENCE, now):
            return 0
        with poller.main.connect() as c:
            todo = self.window_games(c, now)
        n = 0
        for g in todo:
            n += len(poller._guard(f"nfl game {g.game_id}", self.check_game, poller, g, now)
                     or [])  # fmt: skip
        return n

    # --- one game ----------------------------------------------------------------------------

    def params(self) -> dict:
        if self._params is None:
            self._params = m12_params()
        return self._params

    def check_game(self, poller, g, now: datetime) -> list[Change]:
        kick = _utc(g.start_time_utc)
        eid = self.espn_event(poller, g, now)
        if eid is None:
            log.info("nfl game %s: no ESPN event id", g.game_id)
            return []
        r = poller.http.get("espn", SUMMARY, params={"event": eid})
        r.raise_for_status()
        summary = r.json()
        team_of = espn_teams(poller.main, summary, g)
        current, listed = read_injuries(summary, team_of)
        released: set[int] = set()
        if now >= kick - ROSTER_FROM:
            for espn_tid, tid in team_of.items():
                rr = poller.http.get("espn", ROSTER.format(e=eid, t=espn_tid))
                if rr.status_code != 200:
                    continue
                if apply_roster(current, rr.json().get("entries") or [], tid):
                    released.add(tid)
        key = f"nfl:status:{g.game_id}"
        with poller.pdb.begin() as p:
            prev = get_state(p, key)
        with poller.main.connect() as c:
            known = m12_known(c, g)
        if prev is not None:  # keep entries this read cannot speak about
            for aid, o in prev.items():
                if aid in current:
                    continue
                if o.get("source") == "roster" and o.get("team") not in released:
                    current[aid] = o
                elif o.get("source") == "injuries" and o.get("team") not in listed:
                    current[aid] = o
        changes = self.diff(poller, g, eid, prev, current, known, now)
        if any(ch.qualifies for ch in changes):
            poller._guard(f"nfl model probs {g.game_id}", self.model_probs, poller, g, prev or {},
                          current, known, changes, now)  # fmt: skip
        by_game: list[tuple[int, Change]] = []
        for ch in changes:
            ev = poller.record_event(
                "nfl", ch.game_id, ch.player_key, ch.player_name, ch.team_id, ch.old_status,
                ch.new_status, ch.prob_before, ch.prob_after, ch.snap_share, ch.position,
                ch.qualifies, ch.report_name, ch.report_at, now, ch.tip,
                {"espn_event": eid, "baseline": ch.baseline, "gsis": ch.gsis})  # fmt: skip
            if ev is not None and ch.qualifies:
                by_game.append((ev, ch))
        with poller.pdb.begin() as p:
            set_state(p, key, current, now)
        if by_game:
            self.news(poller, g, eid, by_game, prev or {}, current, known, now)
        return changes

    def model_probs(self, poller, g, prev: dict, current: dict, known: dict,
                    changes: list, now: datetime) -> None:  # fmt: skip
        """The value test against what the model knew (REVIEW M17-0): the ESPN mapping is only
        a pre-filter. M12's own play probability before and after (the game-day roster, the
        nflverse report and this ESPN read, as the re-prediction sees them) must move by
        >= 0.25. A change the model already knew (an inactive already on the game-day roster)
        is recorded, not qualifying, and so is a player M12 does not use (not a candidate)."""
        models = self.models(poller.main, now)
        before = models.play_probs(g, observations(prev, known), now)
        after = models.play_probs(g, observations(current, known), now)
        if after is None:
            return
        for ch in changes:
            if not ch.qualifies:
                continue
            if not ch.gsis or ch.gsis not in after:
                ch.qualifies = False
                continue
            pb, pa = before.get(ch.gsis, ch.prob_before), after[ch.gsis]
            ch.prob_before, ch.prob_after = round(pb, 4), round(pa, 4)
            ch.qualifies = qualifies_nfl(ch.position, ch.snap_share, pb, pa,
                                         starter=ch.position in STARTER_GROUPS)  # fmt: skip

    def diff(self, poller, g, eid: str, prev: dict | None, current: dict, known: dict,
             now: datetime) -> list[Change]:  # fmt: skip
        params = self.params()
        shares = self.snap_shares(poller.main, g, now)
        out = []
        for aid in sorted(set(current) | set(prev or {})):
            new = current.get(aid)
            old = (prev or {}).get(aid)
            name = (new or old or {}).get("name") or aid
            team = (new or old or {}).get("team")
            m = known.get((team, norm_name(name)), {})
            practice = m.get("practice")
            new_st = new["status"] if new else None
            if prev is None:  # baseline: against what M12 knew
                old_st, old_report = m.get("status"), m.get("status")
            else:
                old_st = old["status"] if old else None
                old_report = (old or {}).get("report") or old_st
            new_report = (new or {}).get("report") or old_report
            if new_st == old_st:
                continue
            pb = prob(params, old_st, practice, old_report)
            pa = prob(params, new_st, practice, new_report)
            if prev is None and abs(pa - pb) < 1e-9:
                continue
            share, pos = shares.get((team, norm_name(name)), (None, None))
            group = POSITION_GROUP.get((pos or (new or old or {}).get("pos") or "").upper())
            q = qualifies_nfl(group, share, pb, pa, starter=group in STARTER_GROUPS)
            src = (new or old or {}).get("source", "injuries")
            at = (_when((new or {}).get("date")) if src == "injuries" else None) or now
            out.append(Change(int(g.game_id), _utc(g.start_time_utc), aid, name, team, old_st,
                              new_st, round(pb, 4), round(pa, 4),
                              None if share is None else round(share, 3), group, q,
                              f"espn:{eid}:{'inactives' if src == 'roster' else 'injuries'}",
                              at, prev is None, m.get("gsis")))  # fmt: skip
        return out

    def news(self, poller, g, eid, evs: list[tuple[int, Change]], prev: dict, current: dict,
             known: dict, now: datetime) -> None:  # fmt: skip
        gid = int(g.game_id)
        first_event = evs[0][0]
        models = poller._guard("nfl models", self.models, poller.main, now)
        rp = None
        if models is not None:
            before = observations(prev, known)
            after = observations(current, known)
            rp = poller._guard(f"nfl repredict {gid}", models.repredict, poller.main, g,
                               before, after, now)  # fmt: skip
        if rp is not None:
            with poller.pdb.begin() as p:
                p.execute(insert(repredictions).values(
                    event_id=first_event, game_id=gid, model_version=rp.model_version,
                    point=rp.point, margin_before=rp.margin_before, margin_after=rp.margin_after,
                    total_before=rp.total_before, total_after=rp.total_after,
                    props=list(rp.props), created_at=now))  # fmt: skip
        poller.open_lag("nfl", gid, first_event, now)
        qs = poller.poll_game("nfl", gid, now, first_event)
        if rp is None:
            return
        players = {c.player_name: SimpleNamespace(
            player_name=c.player_name, team_id=c.team_id,
            new_status="Available" if c.new_status == ACTIVE else c.new_status)
            for _, c in evs}  # fmt: skip
        why = "; ".join(f"{c.player_name} {c.old_status or 'not listed'} -> "
                        f"{c.new_status or 'not listed'}" for _, c in evs)  # fmt: skip
        stated = evs[0][1].report_at.astimezone(EASTERN).strftime("%-I:%M %p ET")
        reason = f"{why} (ESPN {stated})"
        poller.signal_news("nfl", gid, first_event, f"ev{first_event}", rp, qs, players, reason,
                           now, models.p_over)  # fmt: skip

    def models(self, engine: Engine, now: datetime):
        if self._models is None:
            self._models = self._models_factory(engine, now)
        return self._models

    # --- lookups -------------------------------------------------------------------------------

    def espn_event(self, poller, g, now: datetime) -> str | None:
        """The game's ESPN event id: stored (`espn_nfl`), else from the scoreboard of its
        Eastern date, matched by teams (recorded in the throwaway copy and the poller state)."""
        from betdata.sources.espn_props import espn_ids, record_nfl_ids

        skey = f"nfl:espn:{g.game_id}"
        with poller.pdb.begin() as p:
            cached = get_state(p, skey)
        if cached:
            return cached
        with poller.main.connect() as c:
            ids = espn_ids(c, "nfl", games.c.game_id == g.game_id)
        if not ids:
            day = _utc(g.start_time_utc).astimezone(EASTERN).date()
            r = poller.http.get("espn", SCOREBOARD, params={"dates": f"{day:%Y%m%d}"})
            r.raise_for_status()
            events = []
            for e in r.json().get("events") or []:
                side = {c["homeAway"]: c["team"] for c in e["competitions"][0]["competitors"]}
                events.append({"id": str(e["id"]), "date": e["date"], **{
                    k: [side[k].get("displayName", ""), side[k].get("abbreviation", "")]
                    for k in ("home", "away")}})  # fmt: skip
            with poller.main.begin() as c:
                record_nfl_ids(c, events)
                ids = espn_ids(c, "nfl", games.c.game_id == g.game_id)
        if not ids:
            return None
        eid = next(iter(ids))
        with poller.pdb.begin() as p:
            set_state(p, skey, eid, now)
        return eid

    def snap_shares(self, engine: Engine, g, now: datetime) -> dict:
        """(team, normalized name) -> (share over his last 4 games played, position)."""
        ck = (int(g.season), now.date())
        if ck not in self._snaps:
            with engine.connect() as c:
                self._snaps = {ck: snap_rows(c, int(g.season), now)}
        return shares_for(self._snaps[ck], (g.home_team_id, g.away_team_id))


def shares_for(rows: dict, team_ids) -> dict:
    """(team, normalized name) -> (mean share over the last 4 games played, last position).
    A name is the player with rows for that team; else, a name that maps to one player only
    (a new arrival brings his role)."""
    out = {}
    for name, rs in rows.items():
        everyone = {r[2] for r in rs}
        for team in team_ids:
            pfr = {r[2] for r in rs if r[1] == team} or (everyone if len(everyone) == 1
                                                          else set())  # fmt: skip
            played = sorted((r for r in rs if r[2] in pfr and r[4] > 0),
                            key=lambda r: r[0])[-SNAP_GAMES:]  # fmt: skip
            if played:
                out[(team, name)] = (sum(r[4] for r in played) / len(played), played[-1][3])
    return out


# --- reading ESPN ----------------------------------------------------------------------------


def espn_teams(engine: Engine, summary: dict, g) -> dict[str, int]:
    """ESPN team id -> our team id (one of the game's two teams), by abbreviation."""
    with engine.connect() as c:
        abbr = dict(c.execute(select(teams.c.abbrev, teams.c.team_id).where(
            teams.c.team_id.in_((g.home_team_id, g.away_team_id)))).all())  # fmt: skip
    found: dict[str, int] = {}
    blocks = [b.get("team") or {} for b in summary.get("injuries") or []]
    for comp in (summary.get("header") or {}).get("competitions") or []:
        blocks += [c.get("team") or {} for c in comp.get("competitors") or []]
    for t in blocks:
        a = str(t.get("abbreviation") or "").upper()
        tid = abbr.get(ESPN_ABBR.get(a, a))
        if t.get("id") is not None and tid is not None:
            found[str(t["id"])] = tid
    return found


def read_injuries(summary: dict, team_of: dict[str, int]) -> tuple[dict, set[int]]:
    """{athlete id: observation} from the summary's injuries, and the teams with a non-empty
    list."""
    out, listed = {}, set()
    for block in summary.get("injuries") or []:
        tid = team_of.get(str((block.get("team") or {}).get("id")))
        if tid is None:
            continue
        for it in block.get("injuries") or []:
            ath = it.get("athlete") or {}
            if ath.get("id") is None or not it.get("status"):
                continue
            listed.add(tid)
            pos = ath.get("position")
            out[str(ath["id"])] = {
                "status": it["status"], "name": ath.get("displayName") or str(ath["id"]),
                "team": tid, "pos": pos.get("abbreviation") if isinstance(pos, dict) else pos,
                "date": it.get("date"), "source": "injuries"}  # fmt: skip
    return out, listed


def apply_roster(current: dict, entries: list[dict], tid: int) -> bool:
    """Game-day inactives from one team's roster entries, only once the list is released
    (>= MIN_ACTIVES active entries). Returns whether it was."""
    actives = sum(1 for e in entries if e.get("active") is True)
    if actives < MIN_ACTIVES:
        return False
    for e in entries:
        pid = str(e.get("playerId") or "")
        if not pid:
            continue
        seen = current.get(pid)
        if e.get("active") is False:
            current[pid] = {"status": INACTIVE, "team": tid, "source": "roster",
                            "name": e.get("displayName") or (seen or {}).get("name") or pid,
                            "pos": (seen or {}).get("pos"), "date": None,
                            "report": (seen or {}).get("status")}  # fmt: skip
        elif e.get("active") is True and seen and seen["status"] in ("Questionable",
                                                                       "Doubtful"):
            current[pid] = {**seen, "status": ACTIVE, "source": "roster",
                            "report": seen["status"], "date": None}  # fmt: skip
    return True


# --- the main database (read-only) ------------------------------------------------------------


def m12_known(conn: Connection, g) -> dict:
    """(team, normalized name) -> {status, practice, gsis} as M12 knew it: the latest nflverse
    report row of the game's week, else a reserve status on the latest weekly roster."""
    out: dict = {}
    teams_ = (g.home_team_id, g.away_team_id)
    for r in conn.execute(select(
            roster_weeks.c.team_id, roster_weeks.c.player_name, roster_weeks.c.gsis_id,
            roster_weeks.c.status, roster_weeks.c.week).where(
            roster_weeks.c.season == g.season, roster_weeks.c.week <= g.week,
            roster_weeks.c.team_id.in_(teams_)).order_by(roster_weeks.c.week)):  # fmt: skip
        st = r.status if r.status and r.status not in ("ACT", "INA") else None
        out[(r.team_id, norm_name(r.player_name))] = {
            "status": "Injured Reserve" if st else None, "practice": None, "gsis": r.gsis_id}
    for r in conn.execute(select(
            injuries.c.team_id, injuries.c.player_name, injuries.c.source_player_id,
            injuries.c.report_status, injuries.c.practice_status).where(
            injuries.c.source == "nflverse", injuries.c.season == g.season,
            injuries.c.week == g.week, injuries.c.team_id.in_(teams_))
            .order_by(injuries.c.fetched_at, injuries.c.injury_id)):  # fmt: skip
        k = (r.team_id, norm_name(r.player_name))
        prev = out.get(k, {})
        out[k] = {"status": r.report_status or prev.get("status"),
                  "practice": r.practice_status,
                  "gsis": r.source_player_id or prev.get("gsis")}  # fmt: skip
    return out


def snap_rows(conn: Connection, season: int, now: datetime) -> dict:
    """normalized name -> [(kickoff, team, pfr id, position, share)] over this and last season,
    games before now."""
    rows = conn.execute(select(
        games.c.start_time_utc, player_game_snaps.c.team_id, player_game_snaps.c.pfr_player_id,
        player_game_snaps.c.position, player_game_snaps.c.player_name,
        player_game_snaps.c.offense_pct, player_game_snaps.c.defense_pct)
        .join(games, games.c.game_id == player_game_snaps.c.game_id)
        .where(player_game_snaps.c.season >= season - 1,
               games.c.start_time_utc < now)).all()  # fmt: skip
    out: dict = {}
    for r in rows:
        group = POSITION_GROUP.get((r.position or "").upper())
        share = (r.offense_pct if group in OFFENSE else r.defense_pct) or 0.0
        out.setdefault(norm_name(r.player_name), []).append(
            (_utc(r.start_time_utc), r.team_id, r.pfr_player_id, r.position, float(share)))
    return out


def m12_params() -> dict:
    """M12's play-probability table: the hourly job's cached training-season params when
    present (data/derived), else the copy stored with the frozen nflp15 fit."""
    import json

    from betdata.nfl.pipeline import TRAIN
    from betdata.nfl.workflow import data_dir

    path = data_dir() / "derived" / f"params_{min(TRAIN)}_{max(TRAIN)}.json"
    if path.exists():
        return json.loads(path.read_text())
    from betdata.nfl.props.live import load_design, load_model

    return load_model(load_design())[2]


# --- re-prediction ------------------------------------------------------------------------------


def observations(statuses: dict, known: dict) -> list[dict]:
    """Status observations with the player's gsis id and practice status (for the store)."""
    out = []
    for o in statuses.values():
        m = known.get((o.get("team"), norm_name(o.get("name") or "")), {})
        if not m.get("gsis"):
            continue
        out.append({**o, "gsis": m["gsis"], "practice": m.get("practice"),
                    "group": POSITION_GROUP.get((o.get("pos") or "").upper())})  # fmt: skip
    return out


def with_statuses(store, game, obs: list[dict], t: datetime):
    """The as-of store with ESPN statuses known one second before t (module docstring)."""
    from betdata.nfl.store import Frame, Store

    known = pd.Timestamp(t) - pd.Timedelta(seconds=1)
    inj, ros = [], []
    rosters = store.frame("rosters").df
    for o in obs:
        st, team = o["status"], o["team"]
        if st == ACTIVE:
            continue
        if st == INACTIVE:
            week = rosters[(rosters.season == game.season) & (rosters.week == game.week)
                           & (rosters.team == team) & (rosters.known_at <= known)]  # fmt: skip
            if len(week):
                ros.append(dict(season=int(game.season), week=int(game.week), team=team,
                                key=o["gsis"], status="INA", group=o.get("group"),
                                position=o.get("pos"), known_at=known))  # fmt: skip
                continue
            st = "Out"
        if is_reserve(st):
            st = "Out"
        rs = st if st in GAME_STATUSES else None
        practice = o.get("practice") or (DEFAULT_PRACTICE[rs] if rs else None)
        inj.append(dict(season=int(game.season), week=int(game.week), team=team, key=o["gsis"],
                        group=o.get("group"), report_status=rs, practice_status=practice,
                        injury_id=-1, known_at=known))  # fmt: skip
    frames = dict(store.frames)
    for name, rows in (("injuries", inj), ("rosters", ros)):
        if rows:
            df = store.frame(name).df
            add = pd.DataFrame(rows)[list(df.columns)] if len(df.columns) else pd.DataFrame(rows)
            frames[name] = Frame(pd.concat([df, add], ignore_index=True))
    return Store(store.games, frames, store.params, names=store.names)


class Models:
    """The frozen NFL page models (side m13w, props nflp15), loaded once per poller job. The
    m13w fit must already be cached (it is never refitted in the poller)."""

    def __init__(self, engine: Engine, now: datetime):
        from betdata.config import PROJECT_ROOT
        from betdata.core.matching import season_for
        from betdata.nfl.data import load_store
        from betdata.nfl.groups import GROUPS
        from betdata.nfl.live import fit_path, frozen_model
        from betdata.nfl.predict import market_weights, version_of
        from betdata.nfl.props.live import load_design, load_model
        from betdata.nfl.selection import Design
        from betdata.nfl.weather import WX_DESIGN_PATH

        self.design = Design.load(WX_DESIGN_PATH)
        seasons = tuple(range(2013, season_for("nfl", now)))
        path = fit_path(self.design, seasons)
        if not path.exists() and not (PROJECT_ROOT / "config" / "fits" / path.name).exists():
            raise FileNotFoundError(f"m13w fit {path.name} not cached; not refitted here")
        with engine.connect() as c:
            store = load_store(c)
        self.store = store.with_params(**m12_params())
        self.fits, _ = frozen_model(self.store, self.design, now)
        self.version = version_of(self.design, self.fits, "m13w")
        self.groups = [GROUPS[n] for n in self.design.groups]
        self.weights = market_weights(self.design)
        self.prop_design = load_design()
        self.prop_model, self.prop_version, self.prop_params = load_model(self.prop_design)

    def repredict(self, engine: Engine, g, before: list[dict], after: list[dict],
                  now: datetime):  # fmt: skip
        from betdata.alerts.nba import Repredicted
        from betdata.nfl.predict import predict_games
        from betdata.nfl.props.live import predict as predict_props

        gid = int(g.game_id)
        game = self.store.games.loc[gid]
        sb, sa = (with_statuses(self.store, game, o, now) for o in (before, after))
        [b] = predict_games(sb, self.design, self.fits, self.groups, [gid], now, self.weights)
        [a] = predict_games(sa, self.design, self.fits, self.groups, [gid], now, self.weights)
        props = []
        with engine.connect() as conn:
            pb = {(p["player_id"], p["market"]): p for p in predict_props(
                conn, now, self.prop_design, store=sb.with_params(**self.prop_params))
                if p["game_id"] == gid}  # fmt: skip
            for p in predict_props(conn, now, self.prop_design,
                                   store=sa.with_params(**self.prop_params)):  # fmt: skip
                key = (p["player_id"], p["market"])
                if p["game_id"] != gid or key not in pb:
                    continue
                props.append(dict(player_name=p["player_name"], player_id=p["player_id"],
                                  team_id=p["team_id"], market=p["market"],
                                  mean_before=pb[key]["mean"], mean_after=p["mean"]))
        return Repredicted(self.version, a.point, b.margin, a.margin, b.total, a.total, props)

    def play_probs(self, g, obs: list[dict], now: datetime) -> dict[str, float] | None:
        """gsis -> the model's own play probability for every candidate of both teams, with
        these ESPN observations in the as-of store: what M12 actually uses at this point."""
        from betdata.nfl.features.availability import candidates
        from betdata.nfl.points import FINAL_REPORT
        from betdata.nfl.predict import EARLY, point_now
        from betdata.nfl.store import AsOf

        game = self.store.games.loc[int(g.game_id)]
        s = with_statuses(self.store, game, obs, now)
        a = AsOf(s, now)
        point = point_now(game.kickoff, now)
        point = FINAL_REPORT if point == EARLY else point
        out: dict[str, float] = {}
        for team in (game.home, game.away):
            for k, _, _, b in candidates(a, game, team, point):
                out[k] = play_prob(s.params, b)
        return out

    def p_over(self, market: str, mean: float, line: float) -> float:
        import numpy as np

        from betdata.nfl.props.live import p_over_no_push

        return float(np.asarray(p_over_no_push(self.prop_model, market, [mean], [line]))[0])
