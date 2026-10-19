"""Load the M12 as-of store from the database (see MODEL.md for each known_at rule)."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
from sqlalchemy import Connection, column, select, table

from betdata.analysis.context import (
    FIRST_WEEKLY_ROSTER_SEASON,
    POSITION_GROUP,
    injury_known_at,
    norm_name,
)
from betdata.core.matching import sport_id
from betdata.db.schema import (
    games,
    injuries,
    markets,
    nfl_game_info,
    nfl_player_game_pbp,
    nfl_team_game_pbp,
    player_game_snaps,
    results,
    roster_weeks,
    teams,
    venues,
    weather_observations,
)
from betdata.nfl.derive import RESULT_DELAY
from betdata.nfl.points import final_report_time, prekick_time
from betdata.nfl.store import FRAMES, Frame, Store

v_odds = table(
    "v_odds_snapshots",
    column("game_id"), column("market_id"), column("game_outcome"), column("line"),
    column("price_american"), column("fetched_at"), column("snapshot_kind"), column("source"),
    column("price_status"),
)  # fmt: skip


def _utc(s: pd.Series) -> pd.Series:
    s = pd.to_datetime(s)
    return s.dt.tz_localize("UTC") if s.dt.tz is None else s.dt.tz_convert("UTC")


def _rest_days(g: pd.DataFrame) -> pd.DataFrame:
    """Days since each team's previous game this season (schedule facts, known in advance)."""
    long = pd.concat(
        [
            g[["game_id", "season", "kickoff", "home"]].rename(columns={"home": "team"}),
            g[["game_id", "season", "kickoff", "away"]].rename(columns={"away": "team"}),
        ]
    ).sort_values(["kickoff", "game_id"])
    long["prev"] = long.groupby(["season", "team"])["kickoff"].shift(1)
    long["rest"] = (long["kickoff"] - long["prev"]).dt.total_seconds() / 86400
    rest = long.set_index(["game_id", "team"])["rest"]
    g = g.copy()
    g["home_rest"] = [rest.get((i, t), np.nan) for i, t in zip(g.game_id, g.home, strict=True)]
    g["away_rest"] = [rest.get((i, t), np.nan) for i, t in zip(g.game_id, g.away, strict=True)]
    for c in ("home_rest", "away_rest"):
        g[c] = g[c].fillna(14.0).round(1)  # first game of the season: treated as rested
    return g


def load_games(conn: Connection) -> pd.DataFrame:
    sid = sport_id(conn, "nfl")
    q = (
        select(
            games.c.game_id, games.c.season, games.c.week, games.c.season_type,
            games.c.start_time_utc.label("kickoff"), games.c.home_team_id.label("home"),
            games.c.away_team_id.label("away"), games.c.neutral_site.label("neutral"),
            games.c.status, games.c.venue_id, venues.c.lat, venues.c.lon,
            venues.c.elevation_m, venues.c.tz, venues.c.country,
            venues.c.roof_type.label("venue_roof"), nfl_game_info.c.div_game,
            nfl_game_info.c.surface, nfl_game_info.c.roof,
        )
        .select_from(
            games.outerjoin(venues, venues.c.venue_id == games.c.venue_id).outerjoin(
                nfl_game_info, nfl_game_info.c.game_id == games.c.game_id
            )
        )
        .where(games.c.sport_id == sid)
    )  # fmt: skip
    g = pd.DataFrame(conn.execute(q).mappings().all())
    g["kickoff"] = _utc(g["kickoff"])
    g["neutral"] = g["neutral"].fillna(False).astype(bool)
    g["week"] = g["week"].fillna(0).astype(int)
    # Roof type known in advance: a dome or outdoor stadium; "retractable" until game day.
    per_game = g["roof"].map({"dome": "dome", "outdoors": "outdoors"})
    venue = g["venue_roof"].map({"dome": "dome", "open": "outdoors", "retractable": "retractable"})
    g["roof_type"] = np.where(g["roof"].isin(["open", "closed"]), "retractable",
                              per_game.fillna(venue).fillna("outdoors"))  # fmt: skip
    g = g.drop(columns=["roof"])
    g = _rest_days(g).sort_values(["kickoff", "game_id"])
    g.index = g["game_id"]
    return g


def load_store(conn: Connection, *, first_season: int = 2010) -> Store:
    g = load_games(conn)
    g = g[g.season >= first_season]
    kick = g["kickoff"]
    frames: dict[str, Frame] = {}

    # Final scores, known 12 h after kickoff.
    r = pd.DataFrame(
        conn.execute(
            select(results.c.game_id, results.c.home_score, results.c.away_score, results.c.source)
            .where(results.c.final.is_(True), results.c.game_id.in_(g.index.tolist()))
        ).mappings().all(),
        columns=["game_id", "home_score", "away_score", "source"],
    )  # fmt: skip
    r = r.dropna(subset=["home_score", "away_score"])
    r = r.sort_values("source").drop_duplicates("game_id", keep="first")  # nflverse first
    r = r[r.game_id.isin(g.index)]
    r["known_at"] = r.game_id.map(kick) + RESULT_DELAY
    frames["results"] = Frame(r[["game_id", "home_score", "away_score", "known_at"]])

    # Coaches named for a game: known by its final injury report. Referee and the roof's
    # open/closed state: known on game day (prekick).
    info = pd.DataFrame(
        conn.execute(select(nfl_game_info.c.game_id, nfl_game_info.c.home_coach,
                            nfl_game_info.c.away_coach, nfl_game_info.c.referee,
                            nfl_game_info.c.roof)).mappings().all(),
        columns=["game_id", "home_coach", "away_coach", "referee", "roof"],
    )  # fmt: skip
    info = info[info.game_id.isin(g.index)]
    coaches = info[["game_id", "home_coach", "away_coach"]].copy()
    coaches["known_at"] = coaches.game_id.map(kick).map(final_report_time)
    frames["coaches"] = Frame(coaches)
    gameday = info[["game_id", "referee"]].copy()
    gameday["roof_state"] = info["roof"].where(info["roof"].isin(["open", "closed"]))
    gameday["known_at"] = gameday.game_id.map(kick).map(prekick_time)
    frames["gameday"] = Frame(gameday)

    frames["team_games"] = Frame(_team_games(conn, g))
    frames["player_games"] = Frame(_player_games(conn, g))
    roster, pfr_to_key, by_name = _rosters(conn, g, first_season)
    frames["rosters"] = Frame(roster)
    frames["snaps"] = Frame(_snaps(conn, g, first_season, pfr_to_key, by_name))
    frames["injuries"] = Frame(_injuries(conn, g, first_season))
    frames["odds"] = Frame(_odds(conn, g))
    frames["weather"] = Frame(_weather(conn, g))
    assert set(frames) == set(FRAMES)
    return Store(g, frames, names=_names(conn, first_season))


def _names(conn: Connection, first_season: int) -> dict[str, str]:
    """Display names for reports only (player key -> name, "team:<id>" -> abbreviation)."""
    out = {f"team:{t}": a or n for t, a, n in conn.execute(
        select(teams.c.team_id, teams.c.abbrev, teams.c.canonical_name))}  # fmt: skip
    for key, name in conn.execute(
        select(roster_weeks.c.gsis_id, roster_weeks.c.player_name)
        .where(roster_weeks.c.season >= first_season, roster_weeks.c.gsis_id.is_not(None))
    ):  # fmt: skip
        out[key] = name
    for key, name in conn.execute(
        select(injuries.c.source_player_id, injuries.c.player_name)
        .where(injuries.c.season >= first_season, injuries.c.source_player_id.is_not(None))
    ):  # fmt: skip
        out.setdefault(key, name)
    return out


def _expand(rows, g: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=[*keys, "season", "week", "kickoff", "known_at"])
    stats = pd.DataFrame(df.pop("stats").tolist())
    df = pd.concat([df.reset_index(drop=True), stats], axis=1)
    df = df[df.game_id.isin(g.index)]
    df["known_at"] = _utc(df["known_at"])
    df["kickoff"] = df.game_id.map(g["kickoff"])
    df["week"] = df.game_id.map(g["week"])
    return df


def _team_games(conn: Connection, g: pd.DataFrame) -> pd.DataFrame:
    rows = conn.execute(
        select(nfl_team_game_pbp.c.game_id, nfl_team_game_pbp.c.season,
               nfl_team_game_pbp.c.team_id.label("team"),
               nfl_team_game_pbp.c.opponent_team_id.label("opp"), nfl_team_game_pbp.c.stats,
               nfl_team_game_pbp.c.known_at)
    ).mappings().all()  # fmt: skip
    df = _expand(rows, g, ["game_id", "team", "opp"])
    if len(df):
        df["home"] = df.team.to_numpy() == df.game_id.map(g["home"]).to_numpy()
        df["neutral"] = df.game_id.map(g["neutral"])
    return df


def _player_games(conn: Connection, g: pd.DataFrame) -> pd.DataFrame:
    rows = conn.execute(
        select(nfl_player_game_pbp.c.game_id, nfl_player_game_pbp.c.season,
               nfl_player_game_pbp.c.team_id.label("team"),
               nfl_player_game_pbp.c.gsis_id.label("key"), nfl_player_game_pbp.c.stats,
               nfl_player_game_pbp.c.known_at)
    ).mappings().all()  # fmt: skip
    return _expand(rows, g, ["game_id", "team", "key"])


def _week_kickoffs(g: pd.DataFrame) -> dict[tuple[int, int, int], pd.Timestamp]:
    out = {}
    for row in g[["season", "week", "home", "away", "kickoff"]].itertuples(index=False):
        out[(row.season, row.week, row.home)] = row.kickoff
        out[(row.season, row.week, row.away)] = row.kickoff
    return out


def _cached(fn):
    """fn memoised on its one argument (player names repeat across ~1M roster rows)."""
    memo: dict = {}

    def call(x):
        if x not in memo:
            memo[x] = fn(x)
        return memo[x]

    return call


def _rosters(conn: Connection, g: pd.DataFrame, first_season: int):
    """Weekly roster rows. Week N is the game-day snapshot: known at the team's week-N
    prekick. Exception: week 1 membership and reserve statuses are set at the final 53-man
    cut ~10 days before, so week 1 rows other than inactives (INA) count from the week-1 final
    report. A week with no game for the team (bye, eliminated) counts from the end of that week.
    Returns (frame, pfr id -> player key, (season, team, name) -> key)."""
    rows = conn.execute(
        select(roster_weeks.c.season, roster_weeks.c.week, roster_weeks.c.team_id.label("team"),
               roster_weeks.c.gsis_id, roster_weeks.c.pfr_id, roster_weeks.c.player_name,
               roster_weeks.c.football_name, roster_weeks.c.last_name, roster_weeks.c.position,
               roster_weeks.c.status)
        .where(roster_weeks.c.season >= first_season)
    )  # fmt: skip
    df = pd.DataFrame(rows.all(), columns=list(rows.keys()))
    pfr_to_key: dict[str, str] = {}
    by_name: dict[tuple[int, int, str], str] = {}
    if df.empty:
        return pd.DataFrame(columns=["season", "week", "team", "key", "status", "group",
                                     "known_at"]), pfr_to_key, by_name  # fmt: skip
    df["key"] = df.gsis_id.where(df.gsis_id.notna(), "pfr:" + df.pfr_id.fillna(""))
    df = df[df.gsis_id.notna() | df.pfr_id.notna()]
    norm = _cached(norm_name)
    ids = df[df.gsis_id.notna()]
    cols = ("gsis_id", "pfr_id", "season", "team", "player_name", "football_name", "last_name")
    for gsis, pfr, season, team, name, football, last in zip(
        *(ids[c].tolist() for c in cols), strict=True
    ):
        if pfr:
            pfr_to_key[pfr] = gsis
        by_name[(season, team, norm(name))] = gsis
        if football and last:
            by_name.setdefault((season, team, norm(f"{football} {last}")), gsis)
    # Before 2016 the "weekly" files are one end-of-season snapshot (REVIEW.md #19): ids only.
    df = df[df.season >= FIRST_WEEKLY_ROSTER_SEASON]
    df["group"] = df.position.fillna("").str.upper().map(POSITION_GROUP)
    kicks = _week_kickoffs(g)
    week_end = g.groupby(["season", "week"])["kickoff"].max() + timedelta(hours=12)
    def known_at(season, week, team, week1_cut):
        k = kicks.get((season, week, team))
        if k is None:
            end = week_end.get((season, week))
            return end if end is not None else pd.Timestamp.max.tz_localize("UTC")
        return final_report_time(k) if week1_cut else prekick_time(k)

    # One computation per (season, week, team, week-1 cut rule), not per player row.
    keys = list(zip(df.season.tolist(), df.week.tolist(), df.team.tolist(),
                    ((df.week == 1) & (df.status != "INA")).tolist(), strict=True))  # fmt: skip
    memo = {k: known_at(*k) for k in set(keys)}
    df["known_at"] = [memo[k] for k in keys]
    df["known_at"] = _utc(df["known_at"])
    cols = ["season", "week", "team", "key", "status", "group", "position", "known_at"]
    return df[cols], pfr_to_key, by_name


def _snaps(conn, g, first_season, pfr_to_key, by_name) -> pd.DataFrame:
    rows = conn.execute(
        select(player_game_snaps.c.game_id, player_game_snaps.c.season,
               player_game_snaps.c.team_id.label("team"), player_game_snaps.c.player_name,
               player_game_snaps.c.pfr_player_id, player_game_snaps.c.position,
               player_game_snaps.c.offense_pct, player_game_snaps.c.defense_pct)
        .where(player_game_snaps.c.game_id.is_not(None),
               player_game_snaps.c.season >= first_season)
    )  # fmt: skip
    df = pd.DataFrame(rows.all(), columns=list(rows.keys()))
    cols = ["game_id", "season", "week", "team", "key", "group", "share", "kickoff", "known_at"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    df = df[df.game_id.isin(g.index)]
    df["group"] = df.position.fillna("").str.upper().map(POSITION_GROUP)
    df = df[df.group.notna()]
    keys = []
    norm = _cached(norm_name)
    for pfr, season, team, name in zip(
        *(df[c].tolist() for c in ("pfr_player_id", "season", "team", "player_name")), strict=True
    ):
        keys.append(pfr_to_key.get(pfr) or by_name.get((season, team, norm(name)))
                    or f"pfr:{pfr}")  # fmt: skip
    df["key"] = keys
    offense = df.group.isin(["QB", "OL", "WR", "TE", "RB"])
    df["share"] = np.where(offense, df.offense_pct.fillna(0.0), df.defense_pct.fillna(0.0))
    df["kickoff"] = df.game_id.map(g["kickoff"])
    df["week"] = df.game_id.map(g["week"])
    df["known_at"] = df["kickoff"] + RESULT_DELAY
    return df[cols]


def _injuries(conn: Connection, g: pd.DataFrame, first_season: int) -> pd.DataFrame:
    rows = conn.execute(
        select(injuries.c.season, injuries.c.week, injuries.c.team_id.label("team"),
               injuries.c.source_player_id.label("key"), injuries.c.position,
               injuries.c.report_status, injuries.c.practice_status, injuries.c.fetched_at,
               injuries.c.report_date, injuries.c.injury_id)
        .where(injuries.c.source == "nflverse", injuries.c.season >= first_season,
               injuries.c.team_id.is_not(None), injuries.c.source_player_id.is_not(None))
    ).mappings().all()  # fmt: skip
    cols = ["season", "week", "team", "key", "group", "report_status", "practice_status",
            "injury_id", "known_at"]  # fmt: skip
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=cols)
    kicks = _week_kickoffs(g)
    df["fetched_at"] = _utc(df["fetched_at"])
    df["report_date"] = _utc(df["report_date"])
    known = []
    for r in df[["season", "week", "team", "fetched_at", "report_date"]].itertuples(index=False):
        k = kicks.get((r.season, r.week, r.team))
        rd = None if pd.isna(r.report_date) else r.report_date
        known.append(r.fetched_at if k is None else injury_known_at(r.fetched_at, rd, k))
    df["known_at"] = _utc(pd.Series(known, index=df.index))
    df["group"] = df.position.fillna("").str.upper().map(POSITION_GROUP)
    return df[cols]


def _odds(conn: Connection, g: pd.DataFrame) -> pd.DataFrame:
    """Polled lines only (never a source's own closing line)."""
    rows = conn.execute(
        select(v_odds.c.game_id, markets.c.key.label("market"), v_odds.c.game_outcome,
               v_odds.c.line, v_odds.c.source, v_odds.c.fetched_at.label("known_at"))
        .join(markets, markets.c.market_id == v_odds.c.market_id)
        .where(v_odds.c.snapshot_kind == "poll", v_odds.c.price_status == "active",
               v_odds.c.game_id.is_not(None), markets.c.period == "game",
               markets.c.key.in_(("spreads", "totals")),
               v_odds.c.game_outcome.in_(("home", "over")))
    ).mappings().all()  # fmt: skip
    df = pd.DataFrame(rows, columns=["game_id", "market", "game_outcome", "line", "source",
                                     "known_at"])  # fmt: skip
    df = df[df.game_id.isin(g.index) & df.line.notna()]
    df["known_at"] = _utc(df["known_at"])
    return df.drop(columns=["game_outcome"])


def _weather(conn: Connection, g: pd.DataFrame) -> pd.DataFrame:
    """Forecasts only: one row per game per forecast run, averaged over the game's hours."""
    w = weather_observations
    rows = conn.execute(
        select(w.c.game_id, w.c.fetched_at, w.c.temp_c, w.c.wind_kph, w.c.gust_kph,
               w.c.precip_mm).where(w.c.kind == "forecast")
    ).mappings().all()  # fmt: skip
    df = pd.DataFrame(rows, columns=["game_id", "fetched_at", "temp_c", "wind_kph", "gust_kph",
                                     "precip_mm"])  # fmt: skip
    df = df[df.game_id.isin(g.index)]
    if df.empty:
        return pd.DataFrame(columns=["game_id", "temp_c", "wind_kph", "gust_kph", "precip_mm",
                                     "known_at"])  # fmt: skip
    df = df.groupby(["game_id", "fetched_at"], as_index=False).mean(numeric_only=True)
    df["known_at"] = _utc(df.pop("fetched_at"))
    return df
