"""Derived NFL tables, rebuilt from files already on disk or in the raw archive (no network).

  nfl_game_info        pre-game facts from the latest archived nflverse games.csv: division
                       game, surface, roof, head coaches, referee
  nfl_team_game_pbp    play-by-play aggregates per team per game (team on offense)
  nfl_player_game_pbp  dropbacks, designed runs and targets per player per game, with EPA

Post-game columns of games.csv (temp, wind, starting QBs, result) are never read. A game's
play-by-play aggregates are stamped known_at = kickoff + 12 h (MODEL.md as-of rules).
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import Connection, delete, desc, insert, select

from betdata.core import raw_store
from betdata.core.matching import sport_id
from betdata.db.schema import (
    data_files,
    game_source_ids,
    games,
    nfl_game_info,
    nfl_player_game_pbp,
    nfl_team_game_pbp,
    raw_responses,
)

log = logging.getLogger(__name__)
NAME = "nflverse"
RESULT_DELAY = timedelta(hours=12)  # a game's stats count as known this long after kickoff
SCHEDULES_ENDPOINT = "/schedules/games.csv"

PBP_COLUMNS = [
    "game_id", "home_team", "away_team", "posteam", "defteam", "play_type", "down", "ydstogo",
    "yardline_100", "qtr", "half_seconds_remaining", "game_seconds_remaining", "wp", "epa",
    "success", "yards_gained", "qb_dropback", "qb_scramble", "rush_attempt", "pass_attempt",
    "sack", "interception", "fumble", "fumble_lost", "fumbled_1_team", "fumble_recovery_1_team",
    "penalty", "penalty_team", "penalty_yards", "pass_oe", "passer_id", "rusher_player_id",
    "receiver_player_id", "qb_epa", "drive", "two_point_attempt",
]  # fmt: skip


def _value(row: dict, key: str) -> str | None:
    v = (row.get(key) or "").strip()
    return None if v in ("", "NA") else v


# --- game info ------------------------------------------------------------------------------


def derive_game_info(conn: Connection) -> int:
    """Replace nfl_game_info from the latest archived games.csv. Returns rows written."""
    raw = conn.execute(
        select(raw_responses.c.raw_id)
        .where(raw_responses.c.source == NAME, raw_responses.c.endpoint == SCHEDULES_ENDPOINT)
        .order_by(desc(raw_responses.c.fetched_at), desc(raw_responses.c.raw_id))
        .limit(1)
    ).scalar()
    if raw is None:
        return 0
    body = raw_store.load(conn, raw)["body"]
    ids = dict(
        conn.execute(
            select(game_source_ids.c.source_game_id, game_source_ids.c.game_id).where(
                game_source_ids.c.source == NAME
            )
        ).all()
    )
    rows = []
    for r in csv.DictReader(io.StringIO(body.decode("utf-8-sig"))):
        game_id = ids.get(r["game_id"])
        if game_id is None:
            continue
        div = _value(r, "div_game")
        rows.append(
            dict(
                game_id=game_id,
                source_game_id=r["game_id"],
                div_game=None if div is None else div == "1",
                surface=_value(r, "surface"),
                roof=_value(r, "roof"),
                home_coach=_value(r, "home_coach"),
                away_coach=_value(r, "away_coach"),
                referee=_value(r, "referee"),
                raw_id=raw,
            )
        )
    conn.execute(delete(nfl_game_info))
    for i in range(0, len(rows), 2000):
        conn.execute(insert(nfl_game_info), rows[i : i + 2000])
    return len(rows)


# --- play-by-play aggregates ------------------------------------------------------------------


def _f(x) -> float:
    return float(x) if x is not None and not (isinstance(x, float) and np.isnan(x)) else 0.0


def team_game_stats(pbp: pd.DataFrame) -> pd.DataFrame:
    """One row per (game, offense team) with counts and sums used by the feature groups."""
    p = pbp[pbp["posteam"].notna() & pbp["play_type"].isin(["pass", "run"])].copy()
    p = p[p["two_point_attempt"].fillna(0) == 0]
    p["epa"] = p["epa"].fillna(0.0)
    p["is_pass"] = p["qb_dropback"].fillna(0) == 1
    p["is_rush"] = ~p["is_pass"]
    p["early"] = p["down"].isin([1, 2])
    p["neutral"] = p["wp"].between(0.2, 0.8) & (p["half_seconds_remaining"] > 120)
    p["explosive"] = (p["is_pass"] & (p["yards_gained"] >= 20)) | (
        p["is_rush"] & (p["yards_gained"] >= 10)
    )
    p["succ"] = p["success"].fillna(0)
    # Seconds the offense took per play: clock between consecutive plays in the same drive.
    p = p.sort_values(["game_id", "game_seconds_remaining"], ascending=[True, False])
    nxt = p.groupby(["game_id", "drive"])["game_seconds_remaining"].shift(-1)
    p["secs"] = (p["game_seconds_remaining"] - nxt).where(lambda s: s.between(1, 60))

    neutral_secs = p["secs"].where(p["neutral"])
    early_neutral = p["early"] & p["neutral"] & p["pass_oe"].notna()
    cols = {
        "plays": 1.0,
        "pass_plays": p["is_pass"],
        "pass_epa": p["epa"] * p["is_pass"],
        "pass_succ": p["succ"] * p["is_pass"],
        "rush_plays": p["is_rush"],
        "rush_epa": p["epa"] * p["is_rush"],
        "rush_succ": p["succ"] * p["is_rush"],
        "early_plays": p["early"],
        "early_epa": p["epa"] * p["early"],
        "explosive": p["explosive"],
        "neutral_plays": p["neutral"],
        "neutral_pass": p["neutral"] & p["is_pass"],
        "neutral_secs": neutral_secs.fillna(0.0),
        "neutral_secs_n": neutral_secs.notna(),
        "early_neutral_n": early_neutral,
        "early_neutral_pass_oe": p["pass_oe"].where(early_neutral).fillna(0.0),
        "pass_att": p["pass_attempt"].fillna(0),
        "ints": p["interception"].fillna(0),
        "fumbles": (p["fumble"] == 1) & (p["fumbled_1_team"] == p["posteam"]),
        "fumbles_lost": p["fumble_lost"].fillna(0),
    }
    frame = pd.DataFrame({k: pd.Series(v, index=p.index).astype(float) for k, v in cols.items()})
    frame["game_id"], frame["posteam"] = p["game_id"], p["posteam"]
    out = frame.groupby(["game_id", "posteam"]).sum()

    # Fourth downs in "go" range: 4th and 1-3 between the opponent's 40 and own 40 (wp 5-95%).
    fourth = pbp[
        (pbp["down"] == 4) & pbp["ydstogo"].between(1, 3) & pbp["yardline_100"].between(40, 60)
        & pbp["wp"].between(0.05, 0.95) & pbp["posteam"].notna()
        & pbp["play_type"].isin(["pass", "run", "punt", "field_goal"])
    ]  # fmt: skip
    went = fourth["play_type"].isin(["pass", "run"]).astype(float)
    f4 = went.groupby([fourth["game_id"], fourth["posteam"]])
    out["fourth_opps"] = f4.size()
    out["fourth_goes"] = f4.sum()

    # Penalties called on the team (any phase), and fumbles in the game it recovered.
    pen = pbp[(pbp["penalty"] == 1) & pbp["penalty_team"].notna()]
    pg = pen.groupby(["game_id", "penalty_team"])
    pen_df = pd.DataFrame({"penalties": pg.size(), "penalty_yards": pg["penalty_yards"].sum()})
    pen_df.index.names = ["game_id", "posteam"]
    out = out.join(pen_df, how="left")
    fum = pbp[(pbp["fumble"] == 1) & pbp["fumble_recovery_1_team"].notna()]
    rec = fum.groupby(["game_id", "fumble_recovery_1_team"]).size().rename("fumble_recoveries")
    rec.index.names = ["game_id", "posteam"]
    out = out.join(rec, how="left")
    tot = fum.groupby("game_id").size().rename("game_fumbles")
    out = out.join(tot, on="game_id", how="left")
    return out.fillna(0.0).reset_index().rename(columns={"posteam": "team"})


def player_game_stats(pbp: pd.DataFrame) -> pd.DataFrame:
    """One row per (game, team, player): dropbacks (passes, scrambles, sacks) with QB EPA,
    designed runs and targets with EPA."""
    p = pbp[pbp["posteam"].notna() & pbp["play_type"].isin(["pass", "run"])]
    p = p[p["two_point_attempt"].fillna(0) == 0]
    db = p[(p["qb_dropback"] == 1) & p["passer_id"].notna()]
    dbg = db.groupby(["game_id", "posteam", "passer_id"])["qb_epa"].agg(["size", "sum"])
    dbg.columns = ["dropbacks", "dropback_epa"]
    ru = p[(p["qb_dropback"] != 1) & (p["rush_attempt"] == 1) & p["rusher_player_id"].notna()]
    rug = ru.groupby(["game_id", "posteam", "rusher_player_id"])["epa"].agg(["size", "sum"])
    rug.columns = ["carries", "carry_epa"]
    tg = p[p["receiver_player_id"].notna() & (p["pass_attempt"] == 1)]
    tgg = tg.groupby(["game_id", "posteam", "receiver_player_id"])["epa"].agg(["size", "sum"])
    tgg.columns = ["targets", "target_epa"]
    for d in (dbg, rug, tgg):
        d.index.names = ["game_id", "team", "gsis_id"]
    out = dbg.join(rug, how="outer").join(tgg, how="outer").fillna(0.0)
    return out.reset_index()


def season_rows(pbp: pd.DataFrame, game_rows, season: int, file_id: int | None):
    """(team rows, player rows) for one season's play-by-play, keyed to our games."""
    by_gid = {r.source_game_id: r for r in game_rows}
    sides = pbp.groupby("game_id")[["home_team", "away_team"]].first()
    teams: dict[tuple[str, str], int] = {}
    for gid, (h, a) in sides.iterrows():
        g = by_gid.get(gid)
        if g is not None:
            teams[(gid, h)], teams[(gid, a)] = g.home_team_id, g.away_team_id

    def common(gid: str, tid: int) -> dict:
        g = by_gid[gid]
        return dict(game_id=g.game_id, season=season, team_id=tid, file_id=file_id,
                    known_at=g.start_time_utc + RESULT_DELAY)  # fmt: skip

    team_rows, player_rows = [], []
    for r in team_game_stats(pbp).to_dict("records"):
        tid = teams.get((r["game_id"], r["team"]))
        if tid is None:
            continue
        g = by_gid[r["game_id"]]
        opp = g.away_team_id if tid == g.home_team_id else g.home_team_id
        stats = {k: _f(v) for k, v in r.items() if k not in ("game_id", "team")}
        team_rows.append(dict(common(r["game_id"], tid), opponent_team_id=opp, stats=stats))
    for r in player_game_stats(pbp).to_dict("records"):
        tid = teams.get((r["game_id"], r["team"]))
        if tid is None:
            continue
        stats = {k: _f(v) for k, v in r.items() if k not in ("game_id", "team", "gsis_id")}
        player_rows.append(dict(common(r["game_id"], tid), gsis_id=r["gsis_id"], stats=stats))
    return team_rows, player_rows


def derive_pbp(conn: Connection, data_dir: Path, seasons: list[int] | None = None) -> int:
    """Rebuild nfl_team_game_pbp / nfl_player_game_pbp for seasons with a stored pbp file."""
    latest: dict[int, tuple[int, str]] = {}
    for f in conn.execute(
        select(data_files.c.file_id, data_files.c.season, data_files.c.path)
        .where(data_files.c.source == NAME, data_files.c.dataset == "pbp")
        .order_by(data_files.c.fetched_at, data_files.c.file_id)
    ):
        latest[f.season] = (f.file_id, f.path)
    sid = sport_id(conn, "nfl")
    written = 0
    for season in sorted(latest):
        if seasons is not None and season not in seasons:
            continue
        file_id, rel = latest[season]
        path = data_dir / rel
        if not path.exists():
            log.warning("pbp file for %s missing at %s; skipped", season, path)
            continue
        pbp = pd.read_parquet(path, columns=PBP_COLUMNS)
        game_rows = conn.execute(
            select(game_source_ids.c.source_game_id, games.c.game_id, games.c.start_time_utc,
                   games.c.home_team_id, games.c.away_team_id)
            .join(games, games.c.game_id == game_source_ids.c.game_id)
            .where(game_source_ids.c.source == NAME, games.c.sport_id == sid,
                   games.c.season == season)
        ).all()  # fmt: skip
        team_rows, player_rows = season_rows(pbp, game_rows, season, file_id)
        for table, rows in ((nfl_team_game_pbp, team_rows), (nfl_player_game_pbp, player_rows)):
            conn.execute(delete(table).where(table.c.season == season))
            for i in range(0, len(rows), 2000):
                conn.execute(insert(table), rows[i : i + 2000])
        written += len(team_rows) + len(player_rows)
        log.info("pbp %s: %d team rows, %d player rows", season, len(team_rows), len(player_rows))
    return written
