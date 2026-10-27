"""M17 B1: the engine's tables, built from nflverse play-by-play (and participation where it
exists), one parquet per season under `<data_dir>/derived/m17/`. No network.

  drives        one row per drive: offence, start field position, result, plays, seconds,
                pass plays, score and clock at the start
  team_games    one row per team per game (offence): plays, dropbacks, sacks, scrambles,
                attempts, designed runs, yards, EPA, neutral-situation counts, drives, points
  player_games  one row per player per game: targets, receptions, yards, air yards, carries,
                red-zone touches, TDs, QB dropback lines, routes and offensive snaps
                (participation; none where the season has no file)

Every row carries `kickoff` and `known_at` = kickoff + 12 h (M12's rule for a game's plays).
Never read here: nflverse `xpass`, `pass_oe`, `vegas_wp`, `spread_line`, `total_line` (they
use the betting line) or `wp` (MODEL.md "M17": neutral = score within 10, own pass curve).
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)
KNOWN_DELAY = pd.Timedelta(hours=12)
VERSION = 2  # bump to rebuild the cached tables
FIRST, LAST = 2016, 2026

PBP_COLS = [
    "game_id", "play_id", "season", "week", "season_type", "home_team", "away_team",
    "posteam", "defteam", "fixed_drive", "fixed_drive_result", "drive_start_yard_line",
    "drive_end_yard_line", "drive_time_of_possession", "drive_start_transition", "play_type",
    "down", "ydstogo",
    "yardline_100", "qtr", "half_seconds_remaining", "game_seconds_remaining",
    "score_differential", "posteam_score", "defteam_score", "qb_dropback", "qb_scramble", "sack",
    "pass_attempt", "rush_attempt", "complete_pass", "interception", "fumble_lost",
    "air_yards", "yards_after_catch", "receiving_yards", "rushing_yards", "passing_yards",
    "yards_gained", "receiver_player_id", "rusher_player_id", "passer_player_id",
    "touchdown", "pass_touchdown", "rush_touchdown", "td_team", "td_player_id", "epa",
    "success", "no_huddle", "field_goal_attempt", "field_goal_result", "kick_distance",
    "kicker_player_id", "extra_point_attempt", "extra_point_result", "two_point_attempt",
    "two_point_conv_result", "punt_attempt", "kickoff_attempt", "return_yards", "touchback",
    "qb_kneel", "qb_spike", "penalty", "safety", "home_score", "away_score",
]  # fmt: skip
RESULTS = ("td", "fg", "missed_fg", "punt", "turnover", "downs", "end_half", "opp_td",
           "safety")  # fmt: skip
RESULT_OF = {"Touchdown": "td", "Field goal": "fg", "Missed field goal": "missed_fg",
             "Punt": "punt", "Turnover": "turnover", "Turnover on downs": "downs",
             "End of half": "end_half", "Opp touchdown": "opp_td", "Safety": "safety"}
NEUTRAL_SCORE = 10  # |score difference| for a neutral play
NEUTRAL_HALF_SECONDS = 120  # outside the last two minutes of a half


def root(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        from betdata.nfl.workflow import data_dir as default

        data_dir = default()
    return Path(data_dir)


def yardline(spot: pd.Series, posteam: pd.Series) -> pd.Series:
    """'KC 25' -> yards to the opponent's goal line for the offence `posteam` (MID 50 -> 50)."""
    parts = spot.fillna("").str.split(" ", n=1, expand=True)
    if parts.shape[1] < 2:
        return pd.Series(np.nan, index=spot.index)
    side, n = parts[0], pd.to_numeric(parts[1], errors="coerce")
    return pd.Series(np.where(side == posteam, 100 - n, n), index=spot.index, dtype=float)


def _clock(v: pd.Series) -> pd.Series:
    mm = v.fillna("").str.split(":", n=1, expand=True)
    if mm.shape[1] < 2:
        return pd.Series(np.nan, index=v.index)
    return pd.to_numeric(mm[0], errors="coerce") * 60 + pd.to_numeric(mm[1], errors="coerce")


def read_pbp(season: int, data_dir: Path | None = None) -> pd.DataFrame:
    path = root(data_dir) / "pbp" / "nfl" / f"play_by_play_{season}.parquet"
    df = pd.read_parquet(path, columns=PBP_COLS)
    return df[df.season_type.isin(["REG", "POST"])].reset_index(drop=True)


def read_participation(season: int, data_dir: Path | None = None) -> pd.DataFrame | None:
    path = (root(data_dir) / "nflverse" / "pbp_participation"
            / f"pbp_participation_{season}.parquet")  # fmt: skip
    if not path.exists():
        return None
    cols = ["nflverse_game_id", "play_id", "offense_players", "was_pressure"]
    p = pd.read_parquet(path, columns=cols)
    return p.rename(columns={"nflverse_game_id": "game_id"})


def flag_plays(pbp: pd.DataFrame) -> pd.DataFrame:
    """Scrimmage-play flags on every row: dropback, sack, scramble, attempt, designed run."""
    d = pbp.copy()
    scrim = d.play_type.isin(["pass", "run"]) & d.posteam.notna()
    d["scrim"] = scrim
    d["dropback"] = scrim & (d.qb_dropback == 1)
    d["is_sack"] = d.dropback & (d.sack == 1)
    d["scramble"] = d.dropback & (d.qb_scramble == 1)
    d["attempt"] = d.dropback & ~d.is_sack & ~d.scramble & (d.pass_attempt == 1)
    d["run"] = scrim & (d.qb_dropback != 1) & (d.rush_attempt == 1)
    d["neutral"] = (scrim & d.down.isin([1, 2])
                    & (d.score_differential.abs() <= NEUTRAL_SCORE)
                    & (d.half_seconds_remaining > NEUTRAL_HALF_SECONDS)
                    & (d.qtr <= 4))  # fmt: skip
    d["rz"] = scrim & (d.yardline_100 <= 20)
    d["gl"] = scrim & (d.yardline_100 <= 5)
    return d


def drives(d: pd.DataFrame) -> pd.DataFrame:
    x = d[d.fixed_drive.notna() & d.posteam.notna()]
    g = x.groupby(["game_id", "fixed_drive"], sort=False)
    first = g.first()
    out = pd.DataFrame({
        "game_id": [k[0] for k in first.index], "drive": [int(k[1]) for k in first.index],
        "season": first.season.values, "week": first.week.values,
        "posteam": first.posteam.values, "defteam": first.defteam.values,
        "home": (first.posteam == first.home_team).values,
        "result": first.fixed_drive_result.map(RESULT_OF).values,
        "start_yl": yardline(first.drive_start_yard_line, first.posteam).values,
        "end_yl": yardline(first.drive_end_yard_line, first.posteam).values,
        "seconds": _clock(first.drive_time_of_possession).values,
        "transition": first.drive_start_transition.values,
        "qtr": first.qtr.values, "half_seconds": first.half_seconds_remaining.values,
        "game_seconds": first.game_seconds_remaining.values,
        "score_diff": first.score_differential.values,
        "plays": g.scrim.sum().values, "dropbacks": g.dropback.sum().values,
        "runs": g.run.sum().values, "sacks": g.is_sack.sum().values,
        "scrambles": g.scramble.sum().values, "attempts": g.attempt.sum().values,
        "yards": g.yards_gained.sum().values,
    })  # fmt: skip
    fg = x[x.field_goal_attempt == 1].groupby(["game_id", "fixed_drive"]).agg(
        fg_dist=("kick_distance", "last"), fg_made=("field_goal_result", "last"))
    out = out.merge(fg, left_on=["game_id", "drive"], right_index=True, how="left")
    out["fg_made"] = out.fg_made == "made"
    out["half"] = np.where(out.qtr <= 2, 1, np.where(out.qtr <= 4, 2, 3))
    # Where the other team's next drive of the same half starts (the field position this
    # drive's end hands over: punt, kickoff after a score, turnover spot, ...).
    out = out.sort_values(["game_id", "drive"]).reset_index(drop=True)
    nxt = out.groupby("game_id").shift(-1)
    same = (nxt.half == out.half) & (nxt.posteam != out.posteam)
    out["next_start"] = nxt.start_yl.where(same)
    out["next_transition"] = nxt.transition.where(same)
    return out


def team_games(d: pd.DataFrame, dr: pd.DataFrame) -> pd.DataFrame:
    s = d[d.scrim]
    g = s.groupby(["game_id", "posteam"])
    neu = s[s.neutral]
    gn = neu.groupby(["game_id", "posteam"])
    t = pd.DataFrame({
        "plays": g.scrim.sum(), "dropbacks": g.dropback.sum(), "sacks": g.is_sack.sum(),
        "scrambles": g.scramble.sum(), "attempts": g.attempt.sum(), "runs": g.run.sum(),
        "completions": g.apply(lambda v: int((v.attempt & (v.complete_pass == 1)).sum())),
        "pass_yards": g.apply(lambda v: float(v.passing_yards[v.attempt].fillna(0).sum())),
        "sack_yards": g.apply(lambda v: float(-v.yards_gained[v.is_sack].fillna(0).sum())),
        "scramble_yards": g.apply(lambda v: float(v.yards_gained[v.scramble].fillna(0).sum())),
        "run_yards": g.apply(lambda v: float(v.rushing_yards[v.run].fillna(0).sum())),
        "interceptions": g.interception.sum(), "fumbles_lost": g.fumble_lost.sum(),
        "pass_epa": g.apply(lambda v: float(v.epa[v.dropback].sum())),
        "run_epa": g.apply(lambda v: float(v.epa[v.run].sum())),
        "pass_success": g.apply(lambda v: float(v.success[v.dropback].sum())),
        "run_success": g.apply(lambda v: float(v.success[v.run].sum())),
        "air_yards": g.apply(lambda v: float(v.air_yards[v.attempt].fillna(0).sum())),
        "neutral_plays": gn.scrim.sum(), "neutral_dropbacks": gn.dropback.sum(),
        "no_huddle": g.no_huddle.sum(),
    }).fillna(0)  # fmt: skip
    # Neutral seconds per play: clock run between consecutive neutral plays of the same drive.
    n2 = neu.sort_values(["game_id", "fixed_drive", "play_id"])
    gap = n2.game_seconds_remaining.shift(1) - n2.game_seconds_remaining
    same = (n2.game_id == n2.game_id.shift(1)) & (n2.fixed_drive == n2.fixed_drive.shift(1))
    gap = gap.where(same & gap.between(1, 60))
    t["neutral_secs"] = gap.groupby([n2.game_id, n2.posteam]).sum()
    t["neutral_gaps"] = gap.notna().groupby([n2.game_id, n2.posteam]).sum()
    t = t.fillna(0)
    dg = dr.groupby(["game_id", "posteam"])
    t["drives"] = dg.size()
    for r in RESULTS:
        t[f"d_{r}"] = dg.result.apply(lambda v, r=r: int((v == r).sum()))
    st = d.groupby(["game_id", "posteam"])
    t["xp_att"] = st.extra_point_attempt.sum()
    t["xp_made"] = st.apply(lambda v: int((v.extra_point_result == "good").sum()))
    t["two_att"] = st.two_point_attempt.sum()
    t["two_made"] = st.apply(lambda v: int((v.two_point_conv_result == "success").sum()))
    t["fg_att"] = st.field_goal_attempt.sum()
    t["fg_made"] = st.apply(lambda v: int((v.field_goal_result == "made").sum()))
    t["punts"] = st.punt_attempt.sum()
    t = t.fillna(0).reset_index().rename(columns={"posteam": "team"})
    fin = d.groupby("game_id").agg(home=("home_team", "first"), away=("away_team", "first"),
                                   home_score=("home_score", "first"),
                                   away_score=("away_score", "first"),
                                   season=("season", "first"), week=("week", "first"))  # fmt: skip
    t = t.merge(fin, left_on="game_id", right_index=True)
    t["is_home"] = t.team == t.home
    t["opp"] = np.where(t.is_home, t.away, t.home)
    t["points"] = np.where(t.is_home, t.home_score, t.away_score)
    t["opp_points"] = np.where(t.is_home, t.away_score, t.home_score)
    return t.drop(columns=["home", "away"])


def player_games(d: pd.DataFrame, part: pd.DataFrame | None) -> pd.DataFrame:
    s = d[d.scrim]
    tg = s[s.attempt & s.receiver_player_id.notna()]
    rec = tg.groupby(["game_id", "posteam", "receiver_player_id"]).agg(
        targets=("attempt", "size"), receptions=("complete_pass", "sum"),
        rec_yards=("receiving_yards", lambda v: float(v.fillna(0).sum())),
        air_yards=("air_yards", lambda v: float(v.fillna(0).sum())),
        yac=("yards_after_catch", lambda v: float(v.fillna(0).sum())),
        rec_td=("pass_touchdown", "sum"), rz_targets=("rz", "sum"), gl_targets=("gl", "sum"),
    )  # fmt: skip
    rec.index = rec.index.set_names(["game_id", "team", "player"])
    ru = s[(s.run | s.scramble) & s.rusher_player_id.notna()]
    rush = ru.groupby(["game_id", "posteam", "rusher_player_id"]).agg(
        carries=("run", "sum"), scrambles=("scramble", "sum"),
        rush_yards=("rushing_yards", lambda v: float(v.fillna(0).sum())),
        rush_td=("rush_touchdown", "sum"), rz_carries=("rz", lambda v: int(v.sum())),
        gl_carries=("gl", lambda v: int(v.sum())),
    )  # fmt: skip
    rush.index = rush.index.set_names(["game_id", "team", "player"])
    qb = s[s.dropback & s.passer_player_id.notna()]
    qbs = qb.groupby(["game_id", "posteam", "passer_player_id"]).agg(
        qb_dropbacks=("dropback", "sum"), qb_sacks=("is_sack", "sum"),
        qb_attempts=("attempt", "sum"),
        qb_completions=("complete_pass", lambda v: int(v.sum())),
        pass_yards=("passing_yards", lambda v: float(v.fillna(0).sum())),
        pass_td=("pass_touchdown", "sum"), ints=("interception", "sum"),
        qb_epa=("epa", "sum"),
    )  # fmt: skip
    qbs.index = qbs.index.set_names(["game_id", "team", "player"])
    out = rec.join(rush, how="outer").join(qbs, how="outer").fillna(0)
    if part is not None:
        rs = _routes(s, part)
        out = out.join(rs, how="outer").fillna(0)
    else:
        out["routes"], out["off_snaps"] = np.nan, np.nan
    out = out.reset_index()
    meta = d.groupby("game_id").agg(season=("season", "first"), week=("week", "first"))
    return out.merge(meta, left_on="game_id", right_index=True)


def _routes(s: pd.DataFrame, part: pd.DataFrame) -> pd.DataFrame:
    """Offensive snaps (scrimmage plays on the field) and routes (dropbacks on the field) per
    player from participation. Every non-QB on the field for a dropback counts as a route
    runner except linemen, who are removed later by position."""
    p = s[["game_id", "play_id", "posteam", "dropback"]].merge(part, on=["game_id", "play_id"])
    p = p[p.offense_players.notna() & (p.offense_players != "")]
    ex = p.assign(player=p.offense_players.str.split(";")).explode("player")
    ex = ex[ex.player.str.len() > 0]
    g = ex.groupby(["game_id", "posteam", "player"]).agg(off_snaps=("play_id", "size"),
                                                         routes=("dropback", "sum"))  # fmt: skip
    g.index = g.index.set_names(["game_id", "team", "player"])
    return g


def build_season(season: int, data_dir: Path | None = None) -> dict[str, pd.DataFrame]:
    from betdata.nfl.nflverse_files import kickoffs

    pbp = flag_plays(read_pbp(season, data_dir))
    part = read_participation(season, data_dir)
    if part is not None:
        pbp = pbp.merge(part[["game_id", "play_id", "was_pressure"]], on=["game_id", "play_id"],
                        how="left")  # fmt: skip
    else:
        pbp["was_pressure"] = np.nan
    dr = drives(pbp)
    tg = team_games(pbp, dr)
    press = pbp[pbp.dropback].groupby(["game_id", "posteam"]).was_pressure.agg(["sum", "count"])
    tg = tg.merge(press.rename(columns={"sum": "pressures", "count": "pressure_n"}),
                  left_on=["game_id", "team"], right_index=True, how="left")  # fmt: skip
    if part is None:
        tg["pressures"], tg["pressure_n"] = np.nan, np.nan
    pg = player_games(pbp, part)
    from betdata.nfl.cli import _engine

    with _engine().connect() as conn:
        kick = kickoffs(conn)
    out = {"drives": dr, "team_games": tg, "player_games": pg}
    for k, df in out.items():
        ko = df.game_id.map(kick)
        df["kickoff"] = pd.to_datetime(ko, utc=True)
        df["known_at"] = df.kickoff + KNOWN_DELAY
        missing = df.kickoff.isna().sum()
        if missing:
            log.warning("m17 %s %s: %d rows with no kickoff (dropped)", k, season, missing)
        out[k] = df[df.kickoff.notna()].reset_index(drop=True)
    return out


def cache_dir(data_dir: Path | None = None) -> Path:
    return root(data_dir) / "derived" / "m17"


def season_tables(season: int, data_dir: Path | None = None,
                  rebuild: bool = False) -> dict[str, pd.DataFrame]:  # fmt: skip
    """The season's three tables, rebuilt when play-by-play is newer than the cache."""
    cd = cache_dir(data_dir)
    paths = {k: cd / f"{k}_{season}_v{VERSION}.parquet"
             for k in ("drives", "team_games", "player_games")}  # fmt: skip
    src = root(data_dir) / "pbp" / "nfl" / f"play_by_play_{season}.parquet"
    fresh = all(p.exists() and (not src.exists() or p.stat().st_mtime >= src.stat().st_mtime)
                for p in paths.values())  # fmt: skip
    if fresh and not rebuild:
        return {k: pd.read_parquet(p) for k, p in paths.items()}
    tables = build_season(season, data_dir)
    cd.mkdir(parents=True, exist_ok=True)
    for k, p in paths.items():
        tables[k].to_parquet(p)
    return tables


def load(seasons, data_dir: Path | None = None) -> dict[str, pd.DataFrame]:
    parts = [season_tables(s, data_dir) for s in seasons]
    return {k: pd.concat([p[k] for p in parts], ignore_index=True) for k in parts[0]}
