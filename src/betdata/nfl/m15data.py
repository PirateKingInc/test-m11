"""M15 NFL side-variable inputs, built from files on disk (play-by-play under data/pbp/nfl and
the nflverse files under data/nflverse); no network, no database writes. MODEL.md "M15 NFL side
variables".

`with_m15(store, conn)` returns the M12 store plus three frames (the M12 frames are untouched):

  m15_tg     one row per team per game, the team's own plays (offense, or kicking/receiving on
             special teams); defense numbers are the opponent's row. known_at = kickoff + 12 h.
  m15_kick   field goals per kicker per game, with the expected makes. known_at as above.
  m15_depth  depth charts (`load_depth_charts`); known_at = the chart's as_of.

Expected rates (field goal by distance, third-down conversion by distance to go, red-zone
touchdown by yard line) are logistic fits on the training seasons only, like M12's play
probabilities; every game's expected count uses those fits.

Pressure on a dropback: participation `was_pressure` (2016-2025). Where a game has none (2026,
before 2016) the team-game falls back to PFR weekly `times_pressured` (2018+), and else to pbp
sacks + QB hits; both fallbacks are scaled to the participation rate on training games that
have both. Time to throw: participation, else NGS weekly passing (attempt-weighted); before 2016
it is missing and the feature regresses to the league mean.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import Connection, select

from betdata.db.schema import game_source_ids, games
from betdata.nfl import nflverse_files as nf
from betdata.nfl.derive import RESULT_DELAY
from betdata.nfl.store import Frame, Store

CANON = {**nf.FRANCHISE, "LAR": "LA"}
PBP_COLS = [
    "game_id", "play_id", "home_team", "away_team", "posteam", "defteam", "play_type", "down",
    "ydstogo", "yardline_100", "epa", "success", "qb_dropback", "rush_attempt", "sack", "qb_hit",
    "penalty", "penalty_team", "penalty_yards", "field_goal_result", "kick_distance",
    "kicker_player_id", "fixed_drive", "fixed_drive_result", "third_down_converted",
    "two_point_attempt",
]  # fmt: skip


@dataclass
class Logistic:
    """P(y=1) = 1 / (1 + exp(-(b0 + b1 x))), fitted by Newton's method."""

    b0: float
    b1: float

    def prob(self, x) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-(self.b0 + self.b1 * np.asarray(x, dtype=float))))

    @classmethod
    def fit(cls, x, y, iters: int = 50) -> Logistic:
        X = np.column_stack([np.ones(len(x)), np.asarray(x, dtype=float)])
        y = np.asarray(y, dtype=float)
        b = np.zeros(2)
        for _ in range(iters):
            p = 1.0 / (1.0 + np.exp(-X @ b))
            step = np.linalg.solve((X * (p * (1 - p))[:, None]).T @ X + 1e-9 * np.eye(2),
                                   X.T @ (y - p))  # fmt: skip
            b += step
            if np.abs(step).max() < 1e-10:
                break
        return cls(float(b[0]), float(b[1]))


# --- per-play pieces ---------------------------------------------------------------------------


def fg_plays(p: pd.DataFrame) -> pd.DataFrame:
    fg = p[(p.play_type == "field_goal") & p.kick_distance.notna() & p.posteam.notna()]
    return fg.assign(made=(fg.field_goal_result == "made").astype(float))


def third_downs(p: pd.DataFrame) -> pd.DataFrame:
    d = p[(p.down == 3) & p.play_type.isin(["pass", "run"]) & (p.ydstogo > 0)
          & (p.two_point_attempt.fillna(0) == 0)]  # fmt: skip
    return d.assign(conv=d.third_down_converted.fillna(0).astype(float),
                    x=np.log(d.ydstogo.astype(float)))  # fmt: skip


def red_zone_trips(p: pd.DataFrame) -> pd.DataFrame:
    """One row per drive that snapped inside the opponent's 20: yard line of its first such
    snap and whether the drive ended in a touchdown."""
    rz = p[(p.yardline_100 <= 20) & p.play_type.isin(["pass", "run"]) & p.posteam.notna()
           & p.fixed_drive.notna()]  # fmt: skip
    first = rz.sort_values("play_id").groupby(["game_id", "fixed_drive"], as_index=False).first()
    return first.assign(td=(first.fixed_drive_result == "Touchdown").astype(float),
                        x=first.yardline_100.astype(float))  # fmt: skip


@dataclass
class Expected:
    fg: Logistic  # x = kick distance (yards)
    third: Logistic  # x = log(yards to go)
    rz: Logistic  # x = yard line of the first red-zone snap
    pressure_from_pfr: float  # participation pressures per PFR times_pressured
    pressure_from_hits: float  # participation pressures per (sacks + QB hits)

    @classmethod
    def from_dict(cls, d: dict) -> Expected:
        return cls(Logistic(**d["fg"]), Logistic(**d["third"]), Logistic(**d["rz"]),
                   d["pressure_from_pfr"], d["pressure_from_hits"])  # fmt: skip

    def to_dict(self) -> dict:
        return {"fg": self.fg.__dict__, "third": self.third.__dict__, "rz": self.rz.__dict__,
                "pressure_from_pfr": self.pressure_from_pfr,
                "pressure_from_hits": self.pressure_from_hits}  # fmt: skip


def fit_expected(pbp: pd.DataFrame, tg: pd.DataFrame) -> Expected:
    """Fits on the plays and team-games given (the caller passes training seasons only)."""
    fg, td, rz = fg_plays(pbp), third_downs(pbp), red_zone_trips(pbp)
    both = tg[(tg.part_n > 0) & (tg.pfr_db > 0)]
    hits = tg[tg.part_n > 0]
    return Expected(
        Logistic.fit(fg.kick_distance, fg.made), Logistic.fit(td.x, td.conv),
        Logistic.fit(rz.x, rz.td),
        float(both.part_pr.sum() / max(both.pfr_pr.sum(), 1.0)),
        float(hits.part_pr.sum() / max((hits.sacks + hits.hits).sum(), 1.0)),
    )  # fmt: skip


# --- team-game table ----------------------------------------------------------------------------


def _sum(df: pd.DataFrame, by: str, cols: dict[str, pd.Series]) -> pd.DataFrame:
    frame = pd.DataFrame({k: pd.Series(v, index=df.index).astype(float) for k, v in cols.items()})
    frame["game_id"], frame["team"] = df["game_id"], df[by]
    return frame.groupby(["game_id", "team"]).sum()


def raw_team_games(p: pd.DataFrame, part: pd.DataFrame, pfr: pd.DataFrame,
                   ngs: pd.DataFrame) -> pd.DataFrame:  # fmt: skip
    """Counts per (nflverse game id, team abbreviation as in pbp), before expected rates."""
    p = p[p.posteam.notna()]
    scrim = p[p.play_type.isin(["pass", "run"]) & (p.two_point_attempt.fillna(0) == 0)]
    db = scrim[scrim.qb_dropback == 1].merge(part, on=["game_id", "play_id"], how="left")
    has_pr = db.was_pressure.notna()
    has_tt = db.time_to_throw.notna()
    car = scrim[(scrim.qb_dropback != 1) & (scrim.rush_attempt == 1)]
    parts = [
        _sum(db, "posteam", {"db": 1.0, "sacks": db.sack.fillna(0), "hits": db.qb_hit.fillna(0),
                             "part_n": has_pr, "part_pr": db.was_pressure.where(has_pr, False),
                             "ttt_n": has_tt, "ttt_sum": db.time_to_throw.fillna(0)}),
        _sum(car, "posteam", {"car": 1.0, "rush_epa": car.epa.fillna(0),
                              "rush_succ": car.success.fillna(0)}),
    ]  # fmt: skip
    punt = p[p.play_type == "punt"]
    ko = p[p.play_type == "kickoff"]
    parts += [
        _sum(punt, "posteam", {"punt_n": 1.0, "punt_epa": punt.epa.fillna(0)}),
        _sum(ko, "defteam", {"ko_n": 1.0, "ko_epa": -ko.epa.fillna(0)}),
        _sum(punt, "defteam", {"ret_n": 1.0, "ret_epa": -punt.epa.fillna(0)}),
        _sum(ko, "posteam", {"ret_n": 1.0, "ret_epa": ko.epa.fillna(0)}),
    ]  # fmt: skip
    pen_plays = p[p.play_type.isin(["pass", "run", "no_play"])
                  & (p.two_point_attempt.fillna(0) == 0)]  # fmt: skip
    acc = (pen_plays.penalty == 1) & (pen_plays.penalty_yards.fillna(0) > 0)
    on_off = acc & (pen_plays.penalty_team == pen_plays.posteam)
    on_def = acc & (pen_plays.penalty_team == pen_plays.defteam)
    parts += [
        _sum(pen_plays, "posteam", {"off_plays": 1.0, "off_pen": on_off,
                                    "off_pen_yds": pen_plays.penalty_yards.where(on_off, 0)}),
        _sum(pen_plays, "defteam", {"def_plays": 1.0, "def_pen": on_def,
                                    "def_pen_yds": pen_plays.penalty_yards.where(on_def, 0)}),
    ]  # fmt: skip
    out = None
    for d in parts:
        out = d if out is None else out.add(d, fill_value=0.0)
    out = out.fillna(0.0).reset_index()
    if len(pfr):
        pr = pfr.assign(team=pfr.team.replace(CANON)).groupby(["game_id", "team"])[
            "times_pressured"].sum().rename("pfr_pr")  # fmt: skip
        out = out.join(pr, on=["game_id", "team"])
    else:
        out["pfr_pr"] = np.nan
    out["pfr_db"] = np.where(out.pfr_pr.notna(), out.db, 0.0)
    out["pfr_pr"] = out.pfr_pr.fillna(0.0)
    out["ngs_ttt"] = np.nan
    if len(ngs):
        n = ngs[(ngs.week > 0) & ngs.attempts.notna()]
        n = n.assign(team=n.team_abbr.replace(CANON), w=n.attempts * n.avg_time_to_throw)
        wk = n.groupby(["season", "week", "team"])[["w", "attempts"]].sum()
        parts_id = out.game_id.str.split("_", expand=True)
        keys = zip(parts_id[0].astype(int), parts_id[1].astype(int), out.team, strict=True)
        vals = [wk.loc[k, "w"] / wk.loc[k, "attempts"] if k in wk.index else np.nan for k in keys]
        out["ngs_ttt"] = vals
    return out


def finish(raw: pd.DataFrame, exp: Expected, pbp: pd.DataFrame) -> pd.DataFrame:
    """Adds expected counts and the pressure / time-to-throw used (with fallbacks)."""
    td, rz = third_downs(pbp), red_zone_trips(pbp)
    td = td.assign(exp=exp.third.prob(td.x))
    rz = rz.assign(exp=exp.rz.prob(rz.x))
    t3 = td.groupby(["game_id", "posteam"]).agg(td3_n=("conv", "size"), td3_conv=("conv", "sum"),
                                                td3_exp=("exp", "sum"))  # fmt: skip
    trz = rz.groupby(["game_id", "posteam"]).agg(rz_n=("td", "size"), rz_td=("td", "sum"),
                                                 rz_exp=("exp", "sum"))  # fmt: skip
    for d in (t3, trz):
        d.index.names = ["game_id", "team"]
    out = raw.join(t3, on=["game_id", "team"]).join(trz, on=["game_id", "team"]).fillna(
        {c: 0.0 for c in ("td3_n", "td3_conv", "td3_exp", "rz_n", "rz_td", "rz_exp")})
    part, pfr = out.part_n > 0, out.pfr_db > 0
    out["press"] = np.where(part, out.part_pr, np.where(
        pfr, exp.pressure_from_pfr * out.pfr_pr, exp.pressure_from_hits * (out.sacks + out.hits)))
    out["press_n"] = np.where(part, out.part_n, out.db)
    out["press_src"] = np.where(part, "participation", np.where(pfr, "pfr", "pbp_hits"))
    ngs = out.ngs_ttt.notna() & ~(out.ttt_n > 0)
    out["ttt_sum"] = np.where(ngs, out.ngs_ttt * out.db, out.ttt_sum)
    out["ttt_n"] = np.where(ngs, out.db, out.ttt_n)
    return out


def kicks(pbp: pd.DataFrame, exp: Expected) -> pd.DataFrame:
    fg = fg_plays(pbp)
    fg = fg[fg.kicker_player_id.notna()].assign(exp=lambda d: exp.fg.prob(d.kick_distance))
    return fg.groupby(["game_id", "posteam", "kicker_player_id"], as_index=False).agg(
        fg_n=("made", "size"), fg_made=("made", "sum"), fg_exp=("exp", "sum")
    ).rename(columns={"posteam": "team", "kicker_player_id": "kicker"})  # fmt: skip


# --- loading -------------------------------------------------------------------------------------


def game_map(conn: Connection) -> pd.DataFrame:
    """nflverse game id -> our game_id, season, kickoff and team ids."""
    rows = conn.execute(
        select(game_source_ids.c.source_game_id, games.c.game_id, games.c.season,
               games.c.home_team_id, games.c.away_team_id)
        .join(games, games.c.game_id == game_source_ids.c.game_id)
        .where(game_source_ids.c.source == "nflverse")
    ).all()  # fmt: skip
    return pd.DataFrame(rows, columns=["nfl_id", "game_id", "season", "home_id", "away_id"])


def _team_ids(gm: pd.DataFrame) -> dict[tuple[int, str], int]:
    """(season, abbreviation) -> team id, from the game ids (abbreviation of the time) and the
    canonical form (pbp and depth charts use LA/LV/LAC in every season)."""
    parts = gm.nfl_id.str.split("_", expand=True)
    out = {}
    for season, away, home, a, h in zip(gm.season, parts[2], parts[3], gm.away_id, gm.home_id,
                                        strict=True):  # fmt: skip
        for abbr, tid in ((away, a), (home, h)):
            out[(int(season), abbr)] = int(tid)
            out[(int(season), CANON.get(abbr, abbr))] = int(tid)
    return out


def read_pbp(data_dir: Path, seasons) -> pd.DataFrame:
    frames = []
    for s in seasons:
        path = Path(data_dir) / "pbp" / "nfl" / f"play_by_play_{s}.parquet"
        if path.exists():
            frames.append(pd.read_parquet(path, columns=PBP_COLS).assign(season=s))
    return pd.concat(frames, ignore_index=True)


def build_frames(store: Store, gm: pd.DataFrame, data_dir: Path, train,
                 expected: Expected | None = None, first_season: int | None = None,
                 cached: bool = False) -> tuple[dict, dict]:  # fmt: skip
    """`expected`: frozen fits (live) instead of fitting on `train`; `first_season`: build only
    seasons from it (live reads the last seasons only); `cached`: read and refresh the
    per-season tables under data/derived (nfl/m15cache.py; needs `expected`) instead of
    reading every file, with the depth charts of the last season only."""
    g = store.games
    seasons = sorted(s for s in set(g.season) if first_season is None or s >= first_season)
    if cached:
        from betdata.nfl.m15cache import cached_depth, cached_tables

        if expected is None:
            raise ValueError("cached m15 inputs need the frozen expected-rate fits")
        exp = expected
        tg, k = cached_tables(data_dir, seasons, exp)
        charts = cached_depth(data_dir, seasons[-1], _kickoffs(gm, g))
        if charts.empty and len(seasons) > 1:
            charts = cached_depth(data_dir, seasons[-2], _kickoffs(gm, g))
    else:
        pbp = read_pbp(data_dir, seasons)
        part = nf.load_participation(seasons, columns=["nflverse_game_id", "play_id",
                                                       "was_pressure", "time_to_throw"],
                                     data_dir=data_dir)  # fmt: skip
        part = part.rename(columns={"nflverse_game_id": "game_id"})
        part["was_pressure"] = part.was_pressure.astype("boolean")
        pfr = nf.load_pfr("pass", seasons, data_dir=data_dir)
        try:
            ngs = nf.load_ngs("passing", data_dir=data_dir)
        except Exception:  # noqa: BLE001 - the file is optional (time-to-throw fallback only)
            ngs = pd.DataFrame()
        raw = raw_team_games(pbp, part, pfr, ngs)
        raw["season"] = raw.game_id.str[:4].astype(int)
        exp = expected or fit_expected(pbp[pbp.season.isin(train)], raw[raw.season.isin(train)])
        tg = finish(raw, exp, pbp)
        k = kicks(pbp, exp)
        charts = nf.load_depth_charts(seasons, kickoffs=_kickoffs(gm, g), data_dir=data_dir)

    ids = gm.set_index("nfl_id")
    ids = ids[ids.game_id.isin(g.index)]
    teams = _team_ids(gm)

    def place(df: pd.DataFrame) -> pd.DataFrame:
        df = df[df.game_id.isin(ids.index)].copy()
        nfl_id = df.game_id
        df["game_id"] = nfl_id.map(ids.game_id).astype(int)
        df["season"] = df.game_id.map(g.season)
        df["team"] = [teams.get((s, t)) for s, t in zip(df.season, df.team, strict=True)]
        df = df[df.team.notna()].copy()
        df["team"] = df.team.astype(int)
        home, away = df.game_id.map(g.home), df.game_id.map(g.away)
        df["opp"] = np.where(df.team == home, away, home)
        df["kickoff"] = df.game_id.map(g.kickoff)
        df["known_at"] = df.kickoff + RESULT_DELAY
        return df

    charts = charts.copy()
    charts["team"] = [teams.get((int(s), CANON.get(t, t))) for s, t in
                      zip(charts.season, charts.team, strict=True)]  # fmt: skip
    charts = charts[charts.team.notna() & charts.as_of.notna() & charts.gsis_id.notna()]
    charts = charts.assign(team=charts.team.astype(int), known_at=charts.as_of)
    depth = charts[["season", "team", "known_at", "position", "group", "rank", "slot",
                    "gsis_id"]].reset_index(drop=True)  # fmt: skip
    depth["rank"] = depth["rank"].fillna(9).astype(int)
    frames = {"m15_tg": Frame(place(tg).drop(columns=["press_src"])),
              "m15_kick": Frame(place(k)), "m15_depth": Frame(depth)}  # fmt: skip
    src = place(tg).groupby(["season", "press_src"]).size().unstack(fill_value=0)
    info = {"expected": exp.to_dict(), "pressure_source_by_season": {
        int(s): {k: int(v) for k, v in r.items()} for s, r in src.iterrows()}}  # fmt: skip
    return frames, info


def _kickoffs(gm: pd.DataFrame, g: pd.DataFrame) -> pd.Series:
    d = gm[gm.game_id.isin(g.index)]
    return pd.Series(d.game_id.map(g.kickoff).to_numpy(), index=d.nfl_id.to_numpy())


def with_m15(store: Store, conn: Connection, data_dir: Path, train, *,
             expected: Expected | None = None, first_season: int | None = None,
             cached: bool = False) -> tuple[Store, dict]:  # fmt: skip
    frames, info = build_frames(store, game_map(conn), data_dir, train, expected, first_season,
                                cached)
    out = Store(store.games, {**store.frames, **frames}, store.params, names=store.names)
    if cached:  # live: M12's frames are the same, so its computed states are reused
        out.cache = store.cache
    return out, info
