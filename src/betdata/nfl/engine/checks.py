"""M17 A2 (route proxy) and A3 (pressure flags): offline checks on the engine's tables.

A3  pressure per dropback from participation (`was_pressure`, 2016-2025) vs Pro Football
    Reference's `times_pressured` (nflverse pfr_advstats, 2018+), per team-game and per
    QB-season: correlation, mean level, and the split-half reliability of each.
A2  next-game targets of WR/TE/RB on 2020-2022, predicted three ways from earlier games only
    (this season and last, shrunk with the A1 paddings): real routes x targets per route,
    proxy routes (snap share x team dropbacks) x targets per proxy route, and target share x
    team attempts. The team volume is the team's own prior average in all three.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from betdata.nfl.engine import stability as S
from betdata.nfl.engine.plays import load, root


def _pfr_pass(seasons, data_dir=None) -> pd.DataFrame:
    frames = []
    for s in seasons:
        p = (root(data_dir) / "nflverse" / "pfr_advstats" / f"advstats_week_pass_{s}.parquet")
        if p.exists():
            frames.append(pd.read_parquet(p, columns=["game_id", "season", "team",
                                                      "pfr_player_id", "times_pressured",
                                                      "times_sacked"]))  # fmt: skip
    return pd.concat(frames, ignore_index=True)


def pressure_check(seasons=tuple(range(2018, 2026)), data_dir=None) -> dict:
    t = load(seasons, data_dir)["team_games"]
    pfr = _pfr_pass(seasons, data_dir)
    team = pfr.groupby(["game_id", "team"]).times_pressured.sum().rename("pfr_pressures")
    tg = t.merge(team, left_on=["game_id", "team"], right_index=True, how="inner")
    tg = tg[(tg.dropbacks > 0) & tg.pressure_n.notna() & (tg.pressure_n > 0)]
    tg["part_rate"] = tg.pressures / tg.dropbacks
    tg["pfr_rate"] = tg.pfr_pressures / tg.dropbacks
    r_game = float(np.corrcoef(tg.part_rate, tg.pfr_rate)[0, 1])
    season = tg.groupby(["team", "season"])[["pressures", "pfr_pressures", "dropbacks"]].sum()
    r_season = float(np.corrcoef(season.pressures / season.dropbacks,
                                 season.pfr_pressures / season.dropbacks)[0, 1])  # fmt: skip
    order = tg.sort_values("kickoff").assign(unit=tg.team, pfr_n=tg.dropbacks)
    order["order"] = order.groupby(["unit", "season"]).cumcount()
    deff = order.assign(unit=order.opp)
    deff["order"] = deff.sort_values("kickoff").groupby(["unit", "season"]).cumcount()
    rel = {}
    for label, df in (("offence", order), ("defence", deff)):
        for name, num in (("participation", "pressures"), ("pfr", "pfr_pressures")):
            m = S.measure(S.Rate(name, label, num, "dropbacks", 120, 300), df)
            rel[f"{label}_{name}"] = {k: m[k] for k in ("r_split_half", "padding",
                                                        "r_season_to_season")}  # fmt: skip
    by_season = tg.groupby("season").agg(part=("pressures", "sum"), pfr=("pfr_pressures", "sum"),
                                         db=("dropbacks", "sum"), games=("game_id", "nunique"))
    by_season = {int(s): {"participation": round(r.part / r.db, 3), "pfr": round(r.pfr / r.db, 3),
                          "team_games": int(len(tg[tg.season == s]))}
                 for s, r in by_season.iterrows()}  # fmt: skip
    return {"team_games": int(len(tg)), "r_team_game": round(r_game, 3),
            "r_team_season": round(r_season, 3), "level_by_season": by_season,
            "reliability": rel}  # fmt: skip


def _prior_sums(df: pd.DataFrame, cols: list[str], half_life: float, last_season_w: float):
    """For each row, exponentially weighted sums of `cols` over the unit's earlier games (this
    season and the last; last season's games times `last_season_w`)."""
    df = df.sort_values("kickoff").reset_index(drop=True)
    out = np.zeros((len(df), len(cols)))
    decay = 0.5 ** (1 / half_life)
    vals = df[cols].to_numpy(float)
    for _, idx in df.groupby("unit").indices.items():
        acc = np.zeros(len(cols))
        prev_season = None
        for i in idx:
            s = df.season.iat[i]
            if prev_season is not None and s != prev_season:
                acc = acc * (last_season_w if s == prev_season + 1 else 0.0)
            out[i] = acc
            acc = acc * decay + np.nan_to_num(vals[i])
            prev_season = s
    return df, pd.DataFrame(out, columns=[f"prior_{c}" for c in cols])


def route_proxy_test(seasons=tuple(range(2019, 2023)), data_dir=None, half_life=4.0,
                     last_season_w=0.5) -> dict:  # fmt: skip
    pads = S.paddings()
    tables = load(seasons, data_dir)
    units = S.unit_games(tables, S.positions(data_dir))["receiver"]
    tg = tables["team_games"].sort_values("kickoff").assign(unit=lambda x: x.team)
    tg, tp = _prior_sums(tg, ["attempts", "dropbacks", "plays"], half_life, last_season_w)
    tg = pd.concat([tg, tp], axis=1)
    tg["prior_games"] = tg.groupby("unit").cumcount()  # used only to require history
    vol = tg.set_index(["game_id", "team"])[["prior_attempts", "prior_dropbacks"]]
    # The team's expected volume next game: its prior weighted average per game.
    w = _prior_sums(tg.assign(one=1.0), ["one"], half_life, last_season_w)[1]
    tg["prior_n"] = w["prior_one"].to_numpy()
    vol = tg.set_index(["game_id", "team"]).assign(
        exp_att=lambda x: x.prior_attempts / x.prior_n.where(x.prior_n > 0),
        exp_db=lambda x: x.prior_dropbacks / x.prior_n.where(x.prior_n > 0))[["exp_att", "exp_db"]]
    u = units.copy()
    u["snap_share"] = u.off_snaps / u.team_plays.where(u.team_plays > 0)
    u, pr = _prior_sums(u, ["targets", "routes", "proxy_routes", "team_attempts",
                            "team_dropbacks", "snap_share"], half_life, last_season_w)
    u = pd.concat([u, pr], axis=1)
    u["prior_games"] = _prior_sums(u.assign(one=1.0), ["one"], half_life, last_season_w)[1][
        "prior_one"].to_numpy()
    u = u.merge(vol, left_on=["game_id", "team"], right_index=True, how="left")
    v = u[u.season.isin((2020, 2021, 2022)) & (u.prior_games >= 2) & u.exp_db.notna()].copy()
    league_tprr = pads[("receiver", "targets_per_route")]["league_mean"]
    k_r = pads[("receiver", "targets_per_route")]["padding"]
    k_p = pads[("receiver", "targets_per_proxy_route")]["padding"]
    k_s = pads[("receiver", "target_share")]["padding"]
    share_mean = pads[("receiver", "target_share")]["league_mean"]
    tprr = (v.prior_targets + k_r * league_tprr) / (v.prior_routes + k_r)
    tppr = (v.prior_targets + k_p * league_tprr) / (v.prior_proxy_routes + k_p)
    route_share = v.prior_routes / v.prior_team_dropbacks.where(v.prior_team_dropbacks > 0)
    snap = v.prior_snap_share / v.prior_games
    share = (v.prior_targets + k_s * share_mean) / (v.prior_team_attempts + k_s)
    preds = {"real_routes": tprr * route_share * v.exp_db,
             "proxy_routes": tppr * snap * v.exp_db,
             "target_share": share * v.exp_att}
    ok = np.ones(len(v), bool)
    for p in preds.values():
        ok &= p.notna().to_numpy()
    y = v.targets.to_numpy()[ok]
    res = {k: {"mae": round(float(np.mean(np.abs(p.to_numpy()[ok] - y))), 4),
               "rmse": round(float(np.sqrt(np.mean((p.to_numpy()[ok] - y) ** 2))), 4),
               "bias": round(float(np.mean(p.to_numpy()[ok] - y)), 4)}
           for k, p in preds.items()}  # fmt: skip
    return {"player_games": int(ok.sum()), "seasons": "2020-2022 (validation)",
            "half_life_games": half_life, "last_season_weight": last_season_w, **res}


def write(out: Path, data_dir=None) -> dict:
    import json

    res = {"pressure": pressure_check(data_dir=data_dir),
           "routes": route_proxy_test(data_dir=data_dir)}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1))
    return res
