"""M17 Part E: score engine runs against results (no line): margin and total MAE, win
probability Brier score, per prediction point and season; and the history bias report of team
volumes (Part C consistency: plays, pass rate, sacks, points, TDs within 3%)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def game_metrics(g: pd.DataFrame, act: pd.DataFrame) -> pd.DataFrame:
    d = g.join(act, on="game_id")
    d = d[d.act_margin.notna()]
    win = (d.act_margin > 0).astype(float) + 0.5 * (d.act_margin == 0)
    rows = []
    for (pt, season), x in list(d.groupby(["point", "season"])) + [
            (("all", "all"), d)]:  # fmt: skip
        w = win.loc[x.index]
        rows.append(dict(point=pt, season=season, games=len(x),
                         margin_mae=float((x.margin_mean - x.act_margin).abs().mean()),
                         margin_mae_median=float((x.margin_median - x.act_margin).abs().mean()),
                         total_mae=float((x.total_mean - x.act_total).abs().mean()),
                         total_bias=float((x.total_mean - x.act_total).mean()),
                         margin_bias=float((x.margin_mean - x.act_margin).mean()),
                         brier=float(((x.p_home_win - w) ** 2).mean()),
                         margin_corr=float(np.corrcoef(x.margin_mean, x.act_margin)[0, 1])))
    return pd.DataFrame(rows)


def volume_bias(g: pd.DataFrame, tg: pd.DataFrame) -> pd.DataFrame:
    """Simulated vs real per-team-game means (both teams), for the history report."""
    cols = {"plays": "plays", "dropbacks": "dropbacks", "sacks": "sacks", "attempts": "attempts",
            "runs": "runs", "pass_yards": "pass_yards", "run_yards": "run_yards",
            "drives": "drives", "td": "d_td", "fg_made": "fg_made"}  # fmt: skip
    real = tg[tg.game_id.isin(g.game_id)]
    out = []
    for k, c in cols.items():
        pred = (g[f"h_{k}"].mean() + g[f"a_{k}"].mean()) / 2
        act = real[c].mean()
        out.append(dict(stat=k, sim=pred, real=act, bias_pct=100 * (pred / act - 1)))
    pts = (g.home_pts.mean() + g.away_pts.mean()) / 2
    out.append(dict(stat="points", sim=pts, real=real.points.mean(),
                    bias_pct=100 * (pts / real.points.mean() - 1)))
    return pd.DataFrame(out)
