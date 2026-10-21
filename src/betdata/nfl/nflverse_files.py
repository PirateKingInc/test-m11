"""Readers for the M15 nflverse files (`betdata backfill --source nflverse_extra`), kept as
parquet under `<data_dir>/nflverse/` at their release paths. No network.

Every loader returns the file's own columns (nflverse names) plus, when `kickoffs` is given
(`kickoffs(conn)`: nflverse game id -> kickoff UTC), the time each row became known (MODEL.md
"As-of rules"; a row whose time can't be placed gets NaT, which `known_at <= t` never keeps):

  game data (participation, FTN, PFR, player stats)  known_at = kickoff + 12 h (as play-by-play)
  NGS weekly rows                                    known_at = the team's game that week + 12 h;
      season totals (week 0) and weeks that match no game: the team's last game of the season
  depth charts                                       as_of, see `load_depth_charts`

Seasons whose file is not on disk are skipped (participation has no 2026 file yet).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pandas as pd
from sqlalchemy import Connection, select

from betdata.db.schema import game_source_ids, games
from betdata.nfl.derive import RESULT_DELAY
from betdata.nfl.points import PREKICK_LEAD

FILES = {
    "participation": "pbp_participation/pbp_participation_{season}.parquet",
    "ftn": "ftn_charting/ftn_charting_{season}.parquet",
    "pfr": "pfr_advstats/advstats_week_{kind}_{season}.parquet",
    "depth_charts": "depth_charts/depth_charts_{season}.parquet",
    "player_stats": "stats_player/stats_player_week_{season}.parquet",
    "ngs": "nextgen_stats/ngs_{kind}.parquet",
}
PFR_KINDS = ("pass", "rush", "rec", "def")
NGS_KINDS = ("passing", "receiving", "rushing")
# NGS labels every season with today's franchise abbreviation (LAR, LV, LAC in 2016); game ids
# use the abbreviation of the time (STL, OAK, SD) and LA for the Rams.
FRANCHISE = {"LAR": "LA", "STL": "LA", "OAK": "LV", "SD": "LAC"}


def _root(data_dir: Path | None) -> Path:
    if data_dir is None:
        from betdata.nfl.workflow import data_dir as default

        data_dir = default()
    return Path(data_dir) / "nflverse"


def _read(paths: Iterable[Path], columns: list[str] | None = None) -> pd.DataFrame:
    frames = [pd.read_parquet(p, columns=columns) for p in paths if p.exists()]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)


def _seasons(dataset: str, seasons: Iterable[int], data_dir, kind: str = "") -> list[Path]:
    root = _root(data_dir)
    return [root / FILES[dataset].format(season=s, kind=kind) for s in sorted(set(seasons))]


def kickoffs(conn: Connection) -> pd.Series:
    """nflverse game id ('2024_01_TEN_CHI') -> kickoff (UTC), from the stored schedule."""
    rows = conn.execute(
        select(game_source_ids.c.source_game_id, games.c.start_time_utc)
        .join(games, games.c.game_id == game_source_ids.c.game_id)
        .where(game_source_ids.c.source == "nflverse")
    ).all()
    s = pd.Series({gid: t for gid, t in rows}, dtype="object")
    return pd.to_datetime(s, utc=True)


def team_games(kick: pd.Series) -> pd.DataFrame:
    """One row per team per game, from the ids' season_week_away_home: season, week, team,
    game_id, kickoff."""
    parts = pd.Series(kick.index, index=kick.index).str.split("_", expand=True)
    base = pd.DataFrame({"game_id": kick.index, "season": parts[0].astype(int).values,
                         "week": parts[1].astype(int).values, "kickoff": kick.values})  # fmt: skip
    return pd.concat([base.assign(team=parts[2].values), base.assign(team=parts[3].values)],
                     ignore_index=True)  # fmt: skip


def _game_known(df: pd.DataFrame, col: str, kick: pd.Series | None) -> pd.DataFrame:
    if kick is not None:
        df["known_at"] = df[col].map(kick) + RESULT_DELAY
    return df


def load_participation(seasons, *, kickoffs: pd.Series | None = None, columns=None,
                       data_dir=None) -> pd.DataFrame:  # fmt: skip
    """One row per play (nflverse_game_id, play_id): possession_team, offense/defense
    formation and personnel, defenders_in_box, number_of_pass_rushers, offense_players and
    defense_players (';'-joined gsis ids), offense/defense_positions, time_to_throw,
    was_pressure, route, defense_man_zone_type, defense_coverage_type. 2016-2025."""
    df = _read(_seasons("participation", seasons, data_dir), columns)
    return _game_known(df, "nflverse_game_id", kickoffs)


def load_ftn(seasons, *, kickoffs: pd.Series | None = None, data_dir=None) -> pd.DataFrame:
    """FTN charting, one row per play (nflverse_game_id, nflverse_play_id, season, week):
    n_blitzers, n_pass_rushers, n_defense_box, is_play_action, is_screen_pass, is_rpo,
    is_motion, is_qb_fault_sack, is_drop, ... 2022+. CC-BY-SA 4.0: "FTN Data via nflverse"."""
    return _game_known(_read(_seasons("ftn", seasons, data_dir)), "nflverse_game_id", kickoffs)


def load_pfr(kind: str, seasons, *, kickoffs: pd.Series | None = None,
             data_dir=None) -> pd.DataFrame:  # fmt: skip
    """PFR advanced stats per player per game (game_id, season, week, team, opponent,
    pfr_player_id). kind: pass (times_pressured, times_blitzed, times_hurried, ...), rush
    (yards before/after contact), rec (drops, broken tackles), def (def_pressures,
    def_times_blitzed, def_targets, def_yards_allowed, ...). 2018+."""
    if kind not in PFR_KINDS:
        raise ValueError(f"kind must be one of {PFR_KINDS}")
    return _game_known(_read(_seasons("pfr", seasons, data_dir, kind)), "game_id", kickoffs)


def load_player_stats(seasons, *, kickoffs: pd.Series | None = None, columns=None,
                      data_dir=None) -> pd.DataFrame:  # fmt: skip
    """Player weekly stats (player_id = gsis id, game_id, season, week, season_type, team,
    opponent_team): passing/rushing/receiving volume, yards and EPA, targets, target_share,
    air_yards_share, FG made/missed by distance, punting, penalties. 1999+."""
    df = _read(_seasons("player_stats", seasons, data_dir), columns)
    return _game_known(df, "game_id", kickoffs)


def load_ngs(kind: str, *, kickoffs: pd.Series | None = None, data_dir=None) -> pd.DataFrame:
    """Next Gen Stats per player per week (season, season_type, week, team_abbr,
    player_gsis_id; week 0 = season totals), all seasons 2016+ in one file. passing:
    avg_time_to_throw, aggressiveness, CPOE; receiving: avg_separation, avg_cushion, YAC over
    expected; rushing: efficiency, rush yards over expected, 8+ defenders in box."""
    if kind not in NGS_KINDS:
        raise ValueError(f"kind must be one of {NGS_KINDS}")
    df = _read([_root(data_dir) / FILES["ngs"].format(kind=kind)])
    if kickoffs is None or df.empty:
        return df
    tg = team_games(kickoffs)
    tg["team"] = tg["team"].replace(FRANCHISE)
    week = tg.groupby(["season", "week", "team"])["kickoff"].min()
    last = tg.groupby(["season", "team"])["kickoff"].max()
    keys = list(zip(df.season, df.week, df.team_abbr.replace(FRANCHISE), strict=True))
    at = pd.Series([week.get(k, pd.NaT) for k in keys], index=df.index)
    fallback = pd.Series([last.get(k[::2], pd.NaT) for k in keys], index=df.index)
    df["known_at"] = pd.to_datetime(at.where(at.notna(), fallback), utc=True) + RESULT_DELAY
    return df


# --- depth charts ----------------------------------------------------------------------------

# Raw slot -> position. Weekly charts (to 2024) use free-text `depth_position`; a value not
# listed here falls back to the player's roster `position`. Daily charts use `pos_abb`.
POSITION = {
    **{p: p for p in ("QB", "RB", "FB", "WR", "TE", "LT", "LG", "C", "RG", "RT", "DE", "DT",
                      "LB", "CB", "S", "K", "P", "LS", "H", "KR", "PR")},
    "HB": "RB", "F": "FB", "T": "OL", "G": "OL", "OT": "OL", "OG": "OL", "OL": "OL",
    "LDE": "DE", "RDE": "DE", "EDGE": "DE", "RUSH": "DE", "LEO": "DE", "LE": "DE", "RE": "DE",
    "LDT": "DT", "RDT": "DT", "NT": "DT", "NG": "DT", "NOSE": "DT", "DL": "DT",
    "ILB": "LB", "OLB": "LB", "MLB": "LB", "LILB": "LB", "RILB": "LB", "LOLB": "LB",
    "ROLB": "LB", "SAM": "LB", "SLB": "LB", "WLB": "LB", "WILL": "LB", "MIKE": "LB", "JACK": "LB",
    "LCB": "CB", "RCB": "CB", "NB": "CB", "NCB": "CB", "NKL": "CB", "NICKE": "CB", "NICK": "CB",
    "NDB": "CB", "N": "CB", "DB": "CB", "FS": "S", "SS": "S",
    "PK": "K", "KO": "K", "KOR": "KR",
}  # fmt: skip
GROUP = {"QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE", "DE": "DL", "DT": "DL",
         "LB": "LB", "CB": "DB", "S": "DB",
         **dict.fromkeys(("LT", "LG", "C", "RG", "RT", "OL"), "OL"),
         **dict.fromkeys(("K", "P", "LS", "H", "KR", "PR"), "ST")}  # fmt: skip
DEPTH_COLUMNS = ["season", "team", "week", "as_of", "position", "group", "rank", "gsis_id",
                 "name", "slot", "format"]  # fmt: skip


def _upper(s: pd.Series) -> pd.Series:
    return s.astype("string").str.strip().str.upper()


def _weekly_as_of(df: pd.DataFrame, kick: pd.Series | None) -> pd.Series:
    """A week-N chart counts from the team's week-N prekick (kickoff - 75 min), the rule for
    week-N rosters: before that, week N-1's chart is the latest. A team with no game that
    week (bye, Super Bowl bye) gets the week's last kickoff."""
    if kick is None or df.empty:
        return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
    tg = team_games(kick)
    own = tg.groupby(["season", "week", "team"])["kickoff"].min() - PREKICK_LEAD
    week_last = tg.groupby(["season", "week"])["kickoff"].max()
    keys = list(zip(df.season, df.week, df.team, strict=True))
    at = pd.Series([own.get(k, week_last.get(k[:2], pd.NaT)) for k in keys], index=df.index)
    return pd.to_datetime(at, utc=True)


def _normalize(raw: pd.DataFrame, season: int, kick: pd.Series | None) -> pd.DataFrame:
    if "dt" in raw:  # daily snapshots (2025+): their own timestamp
        slot = _upper(raw["pos_abb"])
        return raw.assign(season=season, team=raw["team"], week=pd.NA, slot=slot,
                          position=slot.map(POSITION), rank=raw["pos_rank"],
                          name=raw["player_name"], as_of=pd.to_datetime(raw["dt"], utc=True),
                          format="daily")  # fmt: skip
    w = raw[raw["week"].notna()]
    slot = _upper(w["depth_position"])
    pos = slot.map(POSITION).fillna(_upper(w["position"]).map(POSITION))
    w = w.assign(season=season, team=w["club_code"], week=w["week"].astype(int), slot=slot,
                 position=pos, rank=pd.to_numeric(w["depth_team"], errors="coerce"),
                 name=w["full_name"], format="weekly")  # fmt: skip
    return w.assign(as_of=_weekly_as_of(w, kick))


def load_depth_charts(seasons, *, kickoffs: pd.Series | None = None,
                      data_dir=None) -> pd.DataFrame:  # fmt: skip
    """Both nflverse depth-chart formats in one frame (DEPTH_COLUMNS):
    season, team, week (weekly format only), as_of (UTC: when this chart was known),
    position (QB RB FB WR TE LT LG C RG RT; OL where the side isn't given; DE DT LB CB S K P
    LS H KR PR), group (QB RB WR TE OL DL LB DB ST), rank (1 = starter), gsis_id, name,
    slot (the raw abbreviation) and format ('weekly' 2001-2024, 'daily' 2025+).

    as_of: a daily snapshot's own `dt`; a weekly chart the rule in `_weekly_as_of` (needs
    `kickoffs`, NaT without). A 3-4 outside linebacker is LB; an 'EDGE'/'RUSH' slot is DE."""
    paths = zip(sorted(set(seasons)), _seasons("depth_charts", seasons, data_dir), strict=True)
    frames = [_normalize(pd.read_parquet(p), s, kickoffs) for s, p in paths if p.exists()]
    if not frames:
        return pd.DataFrame(columns=DEPTH_COLUMNS)
    df = pd.concat([f.assign(group=f["position"].map(GROUP))[DEPTH_COLUMNS] for f in frames],
                   ignore_index=True)  # fmt: skip
    df["rank"] = df["rank"].astype("Int64")
    df["week"] = df["week"].astype("Int64")
    return (df.sort_values(["as_of", "team", "position", "rank"], kind="stable")
              .drop_duplicates(["season", "team", "week", "as_of", "position", "gsis_id"])
              .reset_index(drop=True))  # fmt: skip


def depth_chart_at(charts: pd.DataFrame, t: pd.Timestamp) -> pd.DataFrame:
    """Each team's most recent chart known at instant `t`."""
    known = charts[charts["as_of"] <= t]
    latest = known.groupby("team")["as_of"].transform("max")
    return known[known["as_of"] == latest].reset_index(drop=True)
