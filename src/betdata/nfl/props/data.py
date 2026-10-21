"""Inputs of the NFL prop model (MODEL.md, M15 NFL player props), each stamped with the time it
became known (a game's stats, snaps, play-by-play and participation: kickoff + 12 h, the M12 rule).

  box    one row per (game, team, player) for QBs, RBs, WRs and TEs who played: nflverse weekly
         player stats (passing, rushing, receiving), snap share (M12's snap counts), red-zone
         targets and carries (play-by-play, yardline_100 <= 20), and from participation
         (2016-2025) pass plays on the field ("routes": on the field on a dropback) and routes
         and receiving yards against man and zone coverage (`defense_man_zone_type`, 2018+).
         A player with snaps but no stat line is in it with zeros.
  team   one row per (game, team): pass attempts, targets, carries, dropbacks, and the man and
         zone snaps its defence played.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import Connection, select

from betdata.db.schema import game_source_ids
from betdata.nfl.derive import RESULT_DELAY
from betdata.nfl.nflverse_files import FRANCHISE, load_participation, load_player_stats

POSITIONS = ("QB", "RB", "WR", "TE")
RZ_YARDLINE = 20
STAT_COLS = {"attempts": "att", "completions": "cmp", "passing_yards": "pass_yds",
             "carries": "car", "rushing_yards": "rush_yds", "rushing_tds": "rush_td",
             "targets": "tgt", "receptions": "rec", "receiving_yards": "rec_yds",
             "receiving_tds": "rec_td", "receiving_air_yards": "air"}  # fmt: skip
COUNTS = (*STAT_COLS.values(), "rz_tgt", "rz_car", "routes", "routes_man", "routes_zone",
          "yds_man", "yds_zone")  # fmt: skip
PBP_COLS = ["game_id", "play_id", "posteam", "defteam", "yardline_100", "qb_dropback",
            "pass_attempt", "rush_attempt", "receiver_player_id", "rusher_player_id",
            "complete_pass", "yards_gained", "two_point_attempt"]  # fmt: skip


def _abbr(s) -> str:
    return FRANCHISE.get(s, s)


def game_keys(conn: Connection, g: pd.DataFrame) -> tuple[dict, dict]:
    """(nflverse game id -> game_id, (nflverse game id, team abbreviation) -> team_id). Team
    abbreviations are normalised to today's franchise (SD -> LAC, ...), as the stats use them."""
    rows = conn.execute(select(game_source_ids.c.source_game_id, game_source_ids.c.game_id)
                        .where(game_source_ids.c.source == "nflverse")).all()  # fmt: skip
    ids, teams = {}, {}
    for nid, gid in rows:
        if gid not in g.index:
            continue
        ids[nid] = gid
        parts = nid.split("_")
        if len(parts) == 4:
            teams[(nid, _abbr(parts[2]))] = g.at[gid, "away"]
            teams[(nid, _abbr(parts[3]))] = g.at[gid, "home"]
    return ids, teams


def _stats(seasons, ids, teams, data_dir) -> pd.DataFrame:
    cols = ["player_id", "player_display_name", "position", "game_id", "team", "season_type",
            *STAT_COLS]  # fmt: skip
    s = load_player_stats(seasons, columns=cols, data_dir=data_dir)
    if s.empty:
        return pd.DataFrame(columns=["nid", "game_id", "team_id", "player_id", "name", "pos",
                                     *STAT_COLS.values()])  # fmt: skip
    s = s[s.season_type.isin(["REG", "POST"])].rename(columns=STAT_COLS)
    s["pos"] = s.position.replace({"FB": "RB", "HB": "RB"})
    s = s[s.pos.isin(POSITIONS)]
    s["nid"] = s.pop("game_id")
    s["team_id"] = [teams.get((n, _abbr(t))) for n, t in zip(s.nid, s.team, strict=True)]
    s["game_id"] = s.nid.map(ids)
    s = s.dropna(subset=["game_id", "team_id"]).rename(columns={"player_display_name": "name"})
    return s[["nid", "game_id", "team_id", "player_id", "name", "pos", *STAT_COLS.values()]]


def _pbp(seasons, data_dir: Path) -> pd.DataFrame:
    frames = []
    for s in seasons:
        p = data_dir / "pbp" / "nfl" / f"play_by_play_{s}.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p, columns=PBP_COLS))
    if not frames:
        return pd.DataFrame(columns=PBP_COLS)
    d = pd.concat(frames, ignore_index=True)
    d = d[d.posteam.notna() & (d.two_point_attempt.fillna(0) == 0)]
    d["posteam"], d["defteam"] = d.posteam.map(_abbr), d.defteam.map(_abbr)
    return d


def _play_counts(pbp: pd.DataFrame, part: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per (nid, player): red-zone targets and carries, routes (on the field for a dropback)
    and routes and receiving yards vs man / zone. Per (nid, team): dropbacks; per (nid, defence):
    man and zone snaps."""
    rz = pbp[pbp.yardline_100 <= RZ_YARDLINE]
    out = [rz[rz.pass_attempt == 1].groupby(["game_id", "receiver_player_id"]).size()
           .rename("rz_tgt").rename_axis(["nid", "player_id"]),
           rz[rz.rush_attempt == 1].groupby(["game_id", "rusher_player_id"]).size()
           .rename("rz_car").rename_axis(["nid", "player_id"])]  # fmt: skip
    db = pbp[pbp.qb_dropback == 1]
    team = db.groupby(["game_id", "posteam"]).size().rename("dropbacks").to_frame()
    team.index.names = ["nid", "team"]
    if len(part):
        pp = part[["nflverse_game_id", "play_id", "offense_players", "defense_man_zone_type"]]
        pp = pp.rename(columns={"nflverse_game_id": "game_id"})
        m = db.merge(pp, on=["game_id", "play_id"])
        mz = m.defense_man_zone_type.fillna("")
        m["cov"] = np.where(mz.str.startswith("MAN"), "man",
                            np.where(mz.str.startswith("ZONE"), "zone", ""))  # fmt: skip
        m["players"] = m.offense_players.fillna("").str.split(";")
        e = m[["game_id", "play_id", "players", "cov", "receiver_player_id", "complete_pass",
               "yards_gained"]].explode("players")  # fmt: skip
        e = e[e.players.str.len() > 0]
        e["yds"] = np.where(e.players == e.receiver_player_id,
                            e.complete_pass.fillna(0) * e.yards_gained.fillna(0), 0.0)  # fmt: skip
        g = e.groupby(["game_id", "players"])
        r = pd.DataFrame({"routes": g.size()})
        for c in ("man", "zone"):
            x = e[e["cov"] == c].groupby(["game_id", "players"])
            r[f"routes_{c}"] = x.size()
            r[f"yds_{c}"] = x.yds.sum()
        r.index.names = ["nid", "player_id"]
        out.append(r)
        d = m[m["cov"] != ""].groupby(["game_id", "defteam", "cov"]).size().unstack(fill_value=0)
        d = d.reindex(columns=["man", "zone"], fill_value=0)
        d.index.names = ["nid", "team"]
        team = team.join(d.rename(columns={"man": "def_man", "zone": "def_zone"}), how="outer")
    players = pd.concat(out, axis=1).fillna(0.0)
    return players.reset_index(), team.reset_index()


def load_box(conn: Connection, g: pd.DataFrame, snaps: pd.DataFrame, seasons,
             data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:  # fmt: skip
    """(box, team) for these seasons. `snaps`: M12's snaps frame (game_id, team, key, group,
    share)."""
    ids, teams = game_keys(conn, g)
    seasons = sorted(set(seasons))
    st = _stats(seasons, ids, teams, data_dir)
    part = load_participation(seasons, data_dir=data_dir,
                              columns=["nflverse_game_id", "play_id", "offense_players",
                                       "defense_man_zone_type"])  # fmt: skip
    pc, tc = _play_counts(_pbp(seasons, data_dir), part)
    nid_of = {v: k for k, v in ids.items()}
    sn = snaps[snaps.group.isin(POSITIONS) & snaps.game_id.isin(st.game_id.unique()
                                                                 if len(st) else [])]  # fmt: skip
    sn = sn[~sn.key.astype(str).str.startswith("pfr:")]
    sn = sn.rename(columns={"team": "team_id", "key": "player_id", "share": "snap",
                            "group": "snap_pos"})[["game_id", "team_id", "player_id", "snap",
                                                   "snap_pos"]]  # fmt: skip
    sn = sn.groupby(["game_id", "team_id", "player_id"], as_index=False).agg(
        snap=("snap", "max"), snap_pos=("snap_pos", "first"))  # fmt: skip
    b = st.merge(sn, on=["game_id", "team_id", "player_id"], how="outer")
    b["nid"] = b.game_id.map(nid_of)
    b["pos"] = b.pos.fillna(b.snap_pos)
    b = b.merge(pc, on=["nid", "player_id"], how="left")
    b = b.reindex(columns=list(dict.fromkeys([*b.columns, *COUNTS])))  # no participation
    b[list(COUNTS)] = b[list(COUNTS)].astype(float).fillna(0.0)
    b["has_part"] = b.nid.isin(set(part.nflverse_game_id)) if len(part) else False
    b["played"] = (b.snap.fillna(0) > 0) | (b[["att", "car", "tgt"]].sum(axis=1) > 0)
    b = b[b.played & b.pos.isin(POSITIONS)].drop(columns=["snap_pos"])
    b["game_id"] = b.game_id.astype(int)
    b["team_id"] = b.team_id.astype(int)
    _context(b, g)
    team = _team(b, tc, teams, g)
    b = b.sort_values(["kickoff", "game_id", "team_id", "player_id"]).reset_index(drop=True)
    return b, team


def _context(b: pd.DataFrame, g: pd.DataFrame) -> None:
    b["season"] = b.game_id.map(g.season)
    b["kickoff"] = b.game_id.map(g.kickoff)
    b["known_at"] = b.kickoff + RESULT_DELAY
    home = b.game_id.map(g.home)
    b["opp_id"] = np.where(b.team_id == home, b.game_id.map(g.away), home).astype(int)
    b["td"] = b.rush_td + b.rec_td


def _team(b: pd.DataFrame, tc: pd.DataFrame, teams: dict, g: pd.DataFrame) -> pd.DataFrame:
    t = b.groupby(["game_id", "team_id"]).agg(
        nid=("nid", "first"), opp_id=("opp_id", "first"), kickoff=("kickoff", "first"),
        known_at=("known_at", "first"), season=("season", "first"), t_att=("att", "sum"),
        t_tgt=("tgt", "sum"), t_car=("car", "sum"), t_air=("air", "sum"))  # fmt: skip
    t = t.reset_index()
    if len(tc):
        tc = tc.assign(team_id=[teams.get((n, x)) for n, x in zip(tc.nid, tc.team, strict=True)])
        tc = tc.dropna(subset=["team_id"]).astype({"team_id": int}).drop(columns="team")
        t = t.merge(tc, on=["nid", "team_id"], how="left")
    for c in ("dropbacks", "def_man", "def_zone"):
        t[c] = t[c].astype(float) if c in t else np.nan
    t["dropbacks"] = t.dropbacks.fillna(t.t_att)
    return t


def load_box_cached(conn: Connection, g: pd.DataFrame, snaps: pd.DataFrame, seasons,
                    data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:  # fmt: skip
    """`load_box` season by season through a cache in `<data_dir>/derived` (kept by the hourly
    runner's state, unlike the nflverse and play-by-play files, which are fetched once a day):
    a season is rebuilt when its stats and play-by-play files are on disk and newer than its
    cache, read from the cache otherwise, and built from what there is when neither exists."""
    boxes, teams = [], []
    for s in sorted(set(seasons)):
        stats = data_dir / "nflverse" / "stats_player" / f"stats_player_week_{s}.parquet"
        pbp = data_dir / "pbp" / "nfl" / f"play_by_play_{s}.parquet"
        cb = data_dir / "derived" / f"nfl_prop_box_{s}.parquet"
        ct = cb.with_name(f"nfl_prop_team_{s}.parquet")
        fresh = stats.exists() and pbp.exists() and (
            not cb.exists() or max(stats.stat().st_mtime, pbp.stat().st_mtime) > cb.stat().st_mtime)
        if cb.exists() and ct.exists() and not fresh:
            boxes.append(pd.read_parquet(cb))
            teams.append(pd.read_parquet(ct))
            continue
        b, t = load_box(conn, g, snaps, [s], data_dir)
        if fresh:
            cb.parent.mkdir(parents=True, exist_ok=True)
            b.to_parquet(cb)
            t.to_parquet(ct)
        boxes.append(b)
        teams.append(t)
    return pd.concat(boxes, ignore_index=True), pd.concat(teams, ignore_index=True)
