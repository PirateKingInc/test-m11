"""M17 A1: how stable is each rate the engine uses (MODEL.md "M17" A1).

For every rate (numerator / denominator, summed over a unit's games):
  split-half      odd vs even games of a unit-season (each half needs `min_half` of the
                  denominator); Pearson r across units, Spearman-Brown to a full season
  padding         k = n·(1 − r)/r with r the split-half r and n the mean half denominator: the
                  opportunities at which a unit's own rate and the prior get equal weight
  season-to-season  r between a unit's rate in consecutive seasons (`min_season` each)

Written once for 2016-2019 (what the engine reads) and once for 2016-2025 (reported only).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

TRAIN = tuple(range(2016, 2020))
ALL = tuple(range(2016, 2026))
SKILL = ("WR", "TE", "RB")


@dataclass(frozen=True)
class Rate:
    name: str
    level: str  # team_off, team_def, receiver, rusher, qb
    num: str
    den: str
    min_half: float
    min_season: float
    note: str = ""


RATES = (
    Rate("pace_sec_per_play", "team_off", "neutral_secs", "neutral_gaps", 60, 150,
         "neutral seconds between plays of a drive"),
    Rate("neutral_pass_rate", "team_off", "neutral_dropbacks", "neutral_plays", 80, 200),
    Rate("sack_rate", "team_off", "sacks", "dropbacks", 120, 300),
    Rate("scramble_rate", "team_off", "scrambles", "dropbacks", 120, 300),
    Rate("pressure_rate", "team_off", "pressures", "pressure_n", 120, 300, "participation"),
    Rate("pass_epa", "team_off", "pass_epa", "dropbacks", 120, 300),
    Rate("run_epa", "team_off", "run_epa", "runs", 80, 200),
    Rate("pass_success", "team_off", "pass_success", "dropbacks", 120, 300),
    Rate("run_success", "team_off", "run_success", "runs", 80, 200),
    Rate("yards_per_attempt", "team_off", "pass_yards", "attempts", 100, 250),
    Rate("yards_per_carry", "team_off", "run_yards", "runs", 80, 200),
    Rate("int_rate", "team_off", "interceptions", "attempts", 100, 250),
    Rate("drive_td_rate", "team_off", "d_td", "drives", 30, 80),
    Rate("drive_fg_rate", "team_off", "d_fg", "drives", 30, 80),
    Rate("drive_punt_rate", "team_off", "d_punt", "drives", 30, 80),
    Rate("drive_turnover_rate", "team_off", "d_turnover", "drives", 30, 80),
    Rate("plays_per_drive", "team_off", "plays", "drives", 30, 80),
    Rate("pace_sec_per_play", "team_def", "neutral_secs", "neutral_gaps", 60, 150),
    Rate("neutral_pass_rate", "team_def", "neutral_dropbacks", "neutral_plays", 80, 200),
    Rate("sack_rate", "team_def", "sacks", "dropbacks", 120, 300),
    Rate("pressure_rate", "team_def", "pressures", "pressure_n", 120, 300, "participation"),
    Rate("pass_epa", "team_def", "pass_epa", "dropbacks", 120, 300),
    Rate("run_epa", "team_def", "run_epa", "runs", 80, 200),
    Rate("yards_per_attempt", "team_def", "pass_yards", "attempts", 100, 250),
    Rate("yards_per_carry", "team_def", "run_yards", "runs", 80, 200),
    Rate("int_rate", "team_def", "interceptions", "attempts", 100, 250),
    Rate("drive_td_rate", "team_def", "d_td", "drives", 30, 80),
    Rate("target_share", "receiver", "targets", "team_attempts", 60, 150,
         "share of team attempts in games played"),
    Rate("route_share", "receiver", "routes", "team_dropbacks", 60, 150, "participation"),
    Rate("targets_per_route", "receiver", "targets", "routes", 60, 150, "participation"),
    Rate("targets_per_proxy_route", "receiver", "targets", "proxy_routes", 60, 150,
         "proxy routes = offensive snap share x team dropbacks"),
    Rate("catch_rate", "receiver", "receptions", "targets", 15, 40),
    Rate("yards_per_target", "receiver", "rec_yards", "targets", 15, 40),
    Rate("adot", "receiver", "air_yards", "targets", 15, 40),
    Rate("yac_per_reception", "receiver", "yac", "receptions", 10, 30),
    Rate("rz_target_share", "receiver", "rz_targets", "team_rz_attempts", 8, 20),
    Rate("rec_td_per_target", "receiver", "rec_td", "targets", 15, 40),
    Rate("carry_share", "rusher", "carries", "team_runs", 40, 100,
         "share of team designed runs in games played"),
    Rate("yards_per_carry", "rusher", "rush_yards_designed", "carries", 30, 75),
    Rate("rz_carry_share", "rusher", "rz_carries", "team_rz_runs", 8, 20),
    Rate("rush_td_per_carry", "rusher", "rush_td", "carries", 30, 75),
    Rate("sack_rate", "qb", "qb_sacks", "qb_dropbacks", 100, 250),
    Rate("scramble_rate", "qb", "scrambles", "qb_dropbacks", 100, 250),
    Rate("completion_rate", "qb", "qb_completions", "qb_attempts", 80, 200),
    Rate("yards_per_attempt", "qb", "pass_yards", "qb_attempts", 80, 200),
    Rate("epa_per_dropback", "qb", "qb_epa", "qb_dropbacks", 100, 250),
    Rate("int_rate", "qb", "ints", "qb_attempts", 80, 200),
)  # fmt: skip


def positions(data_dir=None) -> pd.Series:
    from betdata.nfl.engine.plays import root

    path = root(data_dir) / "nflverse" / "players" / "players.parquet"
    if not path.exists():  # live runner: positions fall back to M12's groups
        return pd.Series(dtype=object)
    p = pd.read_parquet(path, columns=["gsis_id", "position"])
    return p.dropna().drop_duplicates("gsis_id").set_index("gsis_id").position


def unit_games(tables: dict[str, pd.DataFrame], pos: pd.Series) -> dict[str, pd.DataFrame]:
    """Per level: one row per unit-game with `unit`, `season`, `order` (game order in the
    season) and every numerator/denominator column."""
    tg = tables["team_games"].copy()
    tg = tg.sort_values("kickoff")
    off = tg.assign(unit=tg.team)
    deff = tg.assign(unit=tg.opp)
    pg = tables["player_games"].merge(
        tg[["game_id", "team", "attempts", "dropbacks", "runs", "plays"]].rename(columns={
            "attempts": "team_attempts", "dropbacks": "team_dropbacks", "runs": "team_runs",
            "plays": "team_plays"}), on=["game_id", "team"])  # fmt: skip
    rz = _team_rz(tables)
    pg = pg.merge(rz, on=["game_id", "team"], how="left")
    pg["position"] = pg.player.map(pos)
    pg["kickoff"] = pg.game_id.map(tg.drop_duplicates("game_id").set_index("game_id").kickoff)
    pg["proxy_routes"] = pg.off_snaps / pg.team_plays.where(pg.team_plays > 0) * pg.team_dropbacks
    pg["rush_yards_designed"] = pg.rush_yards  # scrambles counted separately below
    rec = pg[pg.position.isin(SKILL) & ((pg.targets > 0) | (pg.routes > 0))]
    rush = pg[pg.position.isin(("RB", "QB", "WR")) & (pg.carries > 0)]
    qb = pg[(pg.position == "QB") & (pg.qb_dropbacks > 0)]
    out = {"team_off": off, "team_def": deff, "receiver": rec.assign(unit=rec.player),
           "rusher": rush.assign(unit=rush.player), "qb": qb.assign(unit=qb.player)}
    for k, df in out.items():
        df = df.sort_values("kickoff")
        df["order"] = df.groupby(["unit", "season"]).cumcount()
        out[k] = df
    return out


def _team_rz(tables) -> pd.DataFrame:
    pg = tables["player_games"]
    g = pg.groupby(["game_id", "team"])
    return pd.DataFrame({"team_rz_attempts": g.rz_targets.sum(),
                         "team_rz_runs": g.rz_carries.sum()}).reset_index()  # fmt: skip


def _corr(a: pd.Series, b: pd.Series) -> float:
    ok = a.notna() & b.notna()
    if ok.sum() < 10:
        return float("nan")
    return float(np.corrcoef(a[ok], b[ok])[0, 1])


def measure(rate: Rate, df: pd.DataFrame) -> dict:
    d = df[df[rate.den].notna() & df[rate.num].notna()]
    d = d.assign(half=d.order % 2)
    h = d.groupby(["unit", "season", "half"])[[rate.num, rate.den]].sum().unstack("half")
    h = h.dropna()
    ok = (h[(rate.den, 0)] >= rate.min_half) & (h[(rate.den, 1)] >= rate.min_half)
    h = h[ok]
    x0 = h[(rate.num, 0)] / h[(rate.den, 0)]
    x1 = h[(rate.num, 1)] / h[(rate.den, 1)]
    r = _corr(x0, x1)
    n_half = float(((h[(rate.den, 0)] + h[(rate.den, 1)]) / 2).mean()) if len(h) else np.nan
    sb = 2 * r / (1 + r) if r == r and r > -1 else np.nan
    k = n_half * (1 - r) / r if r == r and r > 0 else np.nan
    s = d.groupby(["unit", "season"])[[rate.num, rate.den]].sum()
    s = s[s[rate.den] >= rate.min_season]
    x = (s[rate.num] / s[rate.den]).rename("x").reset_index()
    nxt = x.assign(season=x.season - 1).rename(columns={"x": "x_next"})
    pair = x.merge(nxt, on=["unit", "season"])
    tot = d[[rate.num, rate.den]].sum()
    return {"rate": rate.name, "level": rate.level, "numerator": rate.num,
            "denominator": rate.den, "league_mean": round(float(tot[rate.num] / tot[rate.den]), 5),
            "units_split_half": int(len(h)), "mean_half_denominator": round(n_half, 1),
            "r_split_half": round(r, 3), "r_full_season": round(sb, 3),
            "padding": round(k, 1) if k == k else None,
            "units_season_pairs": int(len(pair)),
            "r_season_to_season": round(_corr(pair.x, pair.x_next), 3),
            "min_half": rate.min_half, "min_season": rate.min_season,
            "note": rate.note}  # fmt: skip


def table(seasons=ALL, data_dir=None) -> pd.DataFrame:
    from betdata.nfl.engine.plays import load

    tables = load(seasons, data_dir)
    units = unit_games(tables, positions(data_dir))
    rows = []
    for label, ss in (("2016-2019", TRAIN), (f"{min(seasons)}-{max(seasons)}", seasons)):
        for rate in RATES:
            df = units[rate.level]
            rows.append({"seasons": label, **measure(rate, df[df.season.isin(ss)])})
    return pd.DataFrame(rows)


def write(path: Path, data_dir=None) -> pd.DataFrame:
    t = table(ALL, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    t.to_csv(path, index=False)
    return t


def paddings(path: Path | None = None) -> dict[tuple[str, str], dict]:
    """(level, rate) -> the 2016-2019 row: what the engine reads."""
    from betdata.config import PROJECT_ROOT

    path = path or PROJECT_ROOT / "reports" / "nfl" / "stability.csv"
    t = pd.read_csv(path)
    t = t[t.seasons == "2016-2019"]
    return {(r.level, r.rate): r._asdict() for r in t.itertuples(index=False)}
