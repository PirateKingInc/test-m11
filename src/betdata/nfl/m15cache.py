"""Compact per-season m15 inputs under `<data_dir>/derived` (kept by the hourly runner's state,
unlike the play-by-play and nflverse files, which exist on the runner only in the run that
downloads them).

  nfl_m15_tg_<season>_<tag>.parquet     per team-game counts (m15data.finish), ~600 rows
  nfl_m15_kick_<season>_<tag>.parquet   field goals per kicker per game
  nfl_m15_depth_<season>.parquet        that season's depth charts, starters' groups only,
                                        a daily snapshot kept only when the team's chart changed

<tag> is a hash of the frozen expected-rate fits, so a new fit never reads old counts. A season
is rebuilt when its play-by-play (or depth-chart) file is on disk and some source file is newer
than the cache; otherwise the cache is read. Without both, the season is missing (the live
guard in nfl/live15.py then refuses to predict)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from betdata.nfl import nflverse_files as nf
from betdata.nfl.m15data import Expected, finish, kicks, raw_team_games, read_pbp

DEPTH_GROUPS = ("RB", "WR", "TE", "OL", "DL", "LB", "DB")


def _tag(exp: Expected) -> str:
    return hashlib.sha1(json.dumps(exp.to_dict(), sort_keys=True).encode()).hexdigest()[:8]


def _stale(cache: Path, sources: list[Path]) -> bool:
    have = [p for p in sources if p.exists()]
    return not cache.exists() or any(p.stat().st_mtime > cache.stat().st_mtime for p in have)


def _sources(data_dir: Path, season: int) -> list[Path]:
    root = Path(data_dir) / "nflverse"
    return [Path(data_dir) / "pbp" / "nfl" / f"play_by_play_{season}.parquet",
            root / nf.FILES["participation"].format(season=season),
            root / nf.FILES["pfr"].format(season=season, kind="pass"),
            root / nf.FILES["ngs"].format(kind="passing")]  # fmt: skip


def season_tables(data_dir: Path, season: int, exp: Expected) -> tuple[pd.DataFrame, ...]:
    """(team-game counts, kicker FGs) of one season from the files, in nflverse ids."""
    pbp = read_pbp(data_dir, [season])
    part = nf.load_participation([season], columns=["nflverse_game_id", "play_id",
                                                    "was_pressure", "time_to_throw"],
                                 data_dir=data_dir)  # fmt: skip
    part = part.rename(columns={"nflverse_game_id": "game_id"})
    part["was_pressure"] = part.was_pressure.astype("boolean")
    pfr = nf.load_pfr("pass", [season], data_dir=data_dir)
    ngs = nf.load_ngs("passing", data_dir=data_dir) if _sources(data_dir, season)[3].exists() \
        else pd.DataFrame()  # fmt: skip
    if len(ngs):
        ngs = ngs[ngs.season == season]
    raw = raw_team_games(pbp, part, pfr, ngs)
    raw["season"] = season
    return finish(raw, exp, pbp), kicks(pbp, exp)


def cached_tables(data_dir: Path, seasons, exp: Expected) -> tuple[pd.DataFrame, pd.DataFrame]:
    tgs, ks = [], []
    out = Path(data_dir) / "derived"
    for s in sorted(set(seasons)):
        ct = out / f"nfl_m15_tg_{s}_{_tag(exp)}.parquet"
        ck = out / f"nfl_m15_kick_{s}_{_tag(exp)}.parquet"
        src = _sources(data_dir, s)
        if src[0].exists() and (_stale(ct, src) or not ck.exists()):
            tg, k = season_tables(data_dir, s, exp)
            out.mkdir(parents=True, exist_ok=True)
            tg.to_parquet(ct)
            k.to_parquet(ck)
        if ct.exists() and ck.exists():
            tgs.append(pd.read_parquet(ct))
            ks.append(pd.read_parquet(ck))
    if not tgs:
        raise ValueError(f"m15 inputs: no play-by-play or cached tables for {sorted(seasons)}")
    return pd.concat(tgs, ignore_index=True), pd.concat(ks, ignore_index=True)


def changed_only(charts: pd.DataFrame) -> pd.DataFrame:
    """Drop each team's snapshots identical to its previous one (daily charts repeat)."""
    if charts.empty:
        return charts
    key = charts.position.astype(str) + ":" + charts["rank"].astype(str) + ":" + \
        charts.gsis_id.astype(str)  # fmt: skip
    sig = (charts.assign(k=key).sort_values("k").groupby(["team", "as_of"]).k
           .agg("|".join).reset_index().sort_values(["team", "as_of"]))  # fmt: skip
    keep = sig[sig.k != sig.groupby("team").k.shift()][["team", "as_of"]]
    return charts.merge(keep, on=["team", "as_of"]).reset_index(drop=True)


def cached_depth(data_dir: Path, season: int, kickoffs: pd.Series | None) -> pd.DataFrame:
    src = Path(data_dir) / "nflverse" / nf.FILES["depth_charts"].format(season=season)
    cache = Path(data_dir) / "derived" / f"nfl_m15_depth_{season}.parquet"
    if src.exists() and _stale(cache, [src]):
        d = nf.load_depth_charts([season], kickoffs=kickoffs, data_dir=data_dir)
        d = changed_only(d[d.group.isin(DEPTH_GROUPS) & d.gsis_id.notna() & d.as_of.notna()])
        cache.parent.mkdir(parents=True, exist_ok=True)
        d.to_parquet(cache)
    return pd.read_parquet(cache) if cache.exists() else pd.DataFrame(columns=nf.DEPTH_COLUMNS)
