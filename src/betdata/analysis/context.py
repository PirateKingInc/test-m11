"""Who is missing: each team's recent starters, and the chance each one sits out a game.

Everything here uses only what is known at the final injury report (see nfl/points.py):
  * starters come from snap counts of the team's earlier games;
  * absence comes from that week's injury report (Out, Doubtful, Questionable), counting only
    rows published by the final-report instant, and from roster status (injured reserve, PUP,
    suspended, or no longer on the team) on the team's latest roster *before* this week. The
    week's own roster is the game-day snapshot (it lists inactives and Saturday moves), so it
    is not known yet (REVIEW.md #1, #2).
Game-day inactives and whether the player actually played are not used.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Connection, select

from betdata.core.matching import sport_id
from betdata.db.schema import (
    games,
    injuries,
    player_game_snaps,
    roster_weeks,
    team_game_stats,
)
from betdata.nfl.points import final_report_time

GROUPS = ("QB", "OL", "WR", "TE", "RB", "DL", "LB", "CB", "S")
OFFENSE_GROUPS = ("QB", "OL", "WR", "TE", "RB")
DEFENSE_GROUPS = ("DL", "LB", "CB", "S")
POSITION_GROUP = {
    "QB": "QB", "T": "OL", "G": "OL", "C": "OL", "OL": "OL", "OT": "OL", "OG": "OL",
    "WR": "WR", "TE": "TE", "RB": "RB", "FB": "RB", "HB": "RB",
    "DE": "DL", "DT": "DL", "NT": "DL", "DL": "DL", "EDGE": "DL",
    "LB": "LB", "OLB": "LB", "ILB": "LB", "MLB": "LB",
    "CB": "CB", "DB": "CB", "FS": "S", "SS": "S", "S": "S", "SAF": "S",
}  # fmt: skip
PRESENT_ROSTER_STATUSES = {"ACT", "INA"}  # INA = present that week; not used as an absence
STARTER_SHARE = 0.5  # share of the unit's snaps that makes a player a starter
WINDOW = 6  # team games looked back over for starters
DEFAULT_OUT_PROB = {"Out": 1.0, "Doubtful": 0.9, "Questionable": 0.25}


def norm_name(name: str) -> str:
    n = re.sub(r"[^a-z ]", "", name.lower().replace("-", " ").replace(".", ""))
    return " ".join(w for w in n.split() if w not in {"jr", "sr", "ii", "iii", "iv", "v"})


@dataclass(frozen=True)
class Starter:
    key: str
    name: str
    group: str
    share: float  # average share of the unit's snaps in games he played


@dataclass(frozen=True)
class Absence:
    starter: Starter
    out_prob: float
    reason: str  # e.g. "Out", "Questionable", "IR/reserve", "left team"


@dataclass
class Context:
    """Per-game team stats, snaps, rosters and injury reports, keyed for fast lookups."""

    stats: dict[int, dict[int, tuple[int, float, int, float]]] = field(default_factory=dict)
    # game_id -> team_id -> [(player key, name, group, offense share, defense share)]
    snaps: dict[int, dict[int, list[tuple[str, str, str, float, float]]]] = field(
        default_factory=dict
    )
    # (season, week, team_id) -> player key -> roster status (game-day snapshot of that week)
    rosters: dict[tuple[int, int, int], dict[str, str]] = field(default_factory=dict)
    # (season, team_id) -> weeks with a roster, ascending
    roster_weeks: dict[tuple[int, int], list[int]] = field(default_factory=dict)
    # (season, week, team_id) -> player key -> latest report status
    reports: dict[tuple[int, int, int], dict[str, str]] = field(default_factory=dict)
    # (season, team_id) -> player key -> first week he was on the team's roster
    rostered: dict[tuple[int, int], dict[str, int]] = field(default_factory=dict)


def load_context(conn: Connection, *, first_season: int = 1999) -> Context:
    ctx = Context()
    for r in conn.execute(
        select(
            team_game_stats.c.game_id, team_game_stats.c.team_id, team_game_stats.c.pass_plays,
            team_game_stats.c.pass_epa, team_game_stats.c.rush_plays, team_game_stats.c.rush_epa,
        ).where(team_game_stats.c.game_id.is_not(None), team_game_stats.c.season >= first_season)
    ):  # fmt: skip
        ctx.stats.setdefault(r.game_id, {})[r.team_id] = (
            r.pass_plays, r.pass_epa or 0.0, r.rush_plays, r.rush_epa or 0.0,
        )  # fmt: skip

    # Player identity: injuries use gsis ids, snaps use PFR ids; rosters carry both.
    pfr_to_gsis: dict[str, str] = {}
    by_name: dict[tuple[int, int, str], str] = {}
    for r in conn.execute(
        select(
            roster_weeks.c.season, roster_weeks.c.week, roster_weeks.c.team_id,
            roster_weeks.c.gsis_id, roster_weeks.c.pfr_id, roster_weeks.c.player_name,
            roster_weeks.c.football_name, roster_weeks.c.last_name, roster_weeks.c.status,
        ).where(roster_weeks.c.season >= first_season)
    ):  # fmt: skip
        key = r.gsis_id or (f"pfr:{r.pfr_id}" if r.pfr_id else None)
        if key is None:
            continue
        if r.gsis_id and r.pfr_id:
            pfr_to_gsis[r.pfr_id] = r.gsis_id
        if r.gsis_id:
            by_name[(r.season, r.team_id, norm_name(r.player_name))] = r.gsis_id
            if r.football_name and r.last_name:
                full = norm_name(f"{r.football_name} {r.last_name}")
                by_name.setdefault((r.season, r.team_id, full), r.gsis_id)
        ctx.rosters.setdefault((r.season, r.week, r.team_id), {})[key] = r.status
        first = ctx.rostered.setdefault((r.season, r.team_id), {})
        first[key] = min(first.get(key, r.week), r.week)
    index_roster_weeks(ctx)

    for r in conn.execute(
        select(
            player_game_snaps.c.game_id, player_game_snaps.c.season, player_game_snaps.c.team_id,
            player_game_snaps.c.player_name, player_game_snaps.c.pfr_player_id,
            player_game_snaps.c.position, player_game_snaps.c.offense_pct,
            player_game_snaps.c.defense_pct,
        ).where(
            player_game_snaps.c.game_id.is_not(None), player_game_snaps.c.season >= first_season
        )
    ):  # fmt: skip
        group = POSITION_GROUP.get((r.position or "").upper())
        if group is None:
            continue  # kickers, punters, long snappers
        key = (
            pfr_to_gsis.get(r.pfr_player_id)
            or by_name.get((r.season, r.team_id, norm_name(r.player_name)))
            or f"pfr:{r.pfr_player_id}"
        )
        ctx.snaps.setdefault(r.game_id, {}).setdefault(r.team_id, []).append(
            (key, r.player_name, group, r.offense_pct or 0.0, r.defense_pct or 0.0)
        )

    # Kickoff of each team's game per week: injury rows count only if published by that
    # game's final-report instant.
    kickoffs: dict[tuple[int, int, int], datetime] = {}
    for g in conn.execute(
        select(games.c.season, games.c.week, games.c.home_team_id, games.c.away_team_id,
               games.c.start_time_utc)
        .where(games.c.sport_id == sport_id(conn, "nfl"), games.c.season >= first_season)
    ):  # fmt: skip
        for team in (g.home_team_id, g.away_team_id):
            kickoffs[(g.season, g.week, team)] = g.start_time_utc

    latest: dict[tuple, tuple] = {}
    for r in conn.execute(
        select(
            injuries.c.season, injuries.c.week, injuries.c.team_id, injuries.c.source_player_id,
            injuries.c.report_status, injuries.c.fetched_at, injuries.c.injury_id,
            injuries.c.report_date,
        ).where(
            injuries.c.source == "nflverse", injuries.c.season >= first_season,
            injuries.c.team_id.is_not(None), injuries.c.source_player_id.is_not(None),
        )
    ):  # fmt: skip
        kickoff = kickoffs.get((r.season, r.week, r.team_id))
        if kickoff is not None and injury_known_at(r.fetched_at, r.report_date, kickoff) > (
            final_report_time(kickoff)
        ):
            continue
        k = (r.season, r.week, r.team_id, r.source_player_id)
        if k not in latest or (r.fetched_at, r.injury_id) > latest[k][0]:
            latest[k] = ((r.fetched_at, r.injury_id), r.report_status)
    for (season, week, team_id, pid), (_, status) in latest.items():
        if status:
            ctx.reports.setdefault((season, week, team_id), {})[pid] = status
    return ctx


def injury_known_at(
    fetched_at: datetime, report_date: datetime | None, kickoff: datetime
) -> datetime:
    """When an injury row became known: the time we fetched it, unless it was loaded after
    the game (a backfill), in which case nflverse's own publish time (`date_modified`). A
    backfilled row without a publish time is assumed to be the final report."""
    if fetched_at < kickoff:
        return fetched_at
    if report_date is not None:
        return report_date
    return final_report_time(kickoff)


def index_roster_weeks(ctx: Context) -> None:
    ctx.roster_weeks = {}
    for season, week, team_id in ctx.rosters:
        ctx.roster_weeks.setdefault((season, team_id), []).append(week)
    for weeks in ctx.roster_weeks.values():
        weeks.sort()


def roster_before(ctx: Context, season: int, week: int, team_id: int) -> dict[str, str] | None:
    """The team's latest roster from an earlier week this season (bye weeks have none)."""
    if ctx.rosters and not ctx.roster_weeks:
        index_roster_weeks(ctx)
    earlier = [w for w in ctx.roster_weeks.get((season, team_id), ()) if w < week]
    return ctx.rosters.get((season, earlier[-1], team_id)) if earlier else None


class StarterTracker:
    """Rolling record of each team's recent snaps, fed one finished game at a time."""

    def __init__(self, ctx: Context, window: int = WINDOW):
        self.ctx = ctx
        self.window = window
        # (season, team_id) -> recent games' snap lists, oldest first
        self.recent: dict[tuple[int, int], list[list[tuple]]] = defaultdict(list)

    def record(self, game_id: int, season: int, team_id: int) -> None:
        rows = self.ctx.snaps.get(game_id, {}).get(team_id)
        if rows:
            games = self.recent[(season, team_id)]
            games.append(rows)
            del games[: -self.window]

    def starters(self, season: int, team_id: int) -> list[Starter]:
        """Players averaging >= STARTER_SHARE of their unit's snaps in the games they played,
        over the team's recent games this season (else the end of last season)."""
        games = self.recent.get((season, team_id)) or self.recent.get((season - 1, team_id))
        if not games:
            return []
        shares: dict[str, list[float]] = defaultdict(list)
        info: dict[str, tuple[str, str]] = {}
        for rows in games:
            for key, name, group, off, dfn in rows:
                share = off if group in OFFENSE_GROUPS else dfn
                if share > 0:
                    shares[key].append(share)
                    info[key] = (name, group)
        need = 1 if len(games) < 3 else 2
        out = []
        for key, s in shares.items():
            avg = sum(s) / len(s)
            if len(s) >= need and avg >= STARTER_SHARE:
                out.append(Starter(key, info[key][0], info[key][1], round(avg, 3)))
        return out


def absences(
    ctx: Context,
    starters: list[Starter],
    season: int,
    week: int,
    team_id: int,
    out_prob: dict[str, float] = DEFAULT_OUT_PROB,
) -> list[Absence]:
    """Starters expected to miss this game, with the chance each one sits out, as known at
    the final injury report."""
    roster = roster_before(ctx, season, week, team_id)
    first_week = ctx.rostered.get((season, team_id), {})
    report = ctx.reports.get((season, week, team_id), {})
    out = []
    for s in starters:
        # A player on this week's injury report is with the team; his report status governs.
        if roster is not None and s.key not in report:
            status = roster.get(s.key)
            if status is not None and status not in PRESENT_ROSTER_STATUSES:
                out.append(Absence(s, 1.0, "IR/reserve" if status == "RES" else status))
                continue
            if status is None and first_week.get(s.key, week) < week:
                # On this team earlier this season but not on its latest roster: released,
                # traded or otherwise gone. (Last season's starters who left in the offseason
                # never count: the new roster is the team now.)
                out.append(Absence(s, 1.0, "not on roster"))
                continue
        status = report.get(s.key)
        if status in out_prob:
            out.append(Absence(s, out_prob[status], status))
    return out


def missing_by_group(absent: list[Absence]) -> dict[str, float]:
    """Snap-weighted starters expected out, per position group (2 starting CBs out = ~2.0)."""
    out = dict.fromkeys(GROUPS, 0.0)
    for a in absent:
        out[a.starter.group] += a.out_prob * a.starter.share
    return out
