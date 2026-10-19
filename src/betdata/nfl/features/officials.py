"""Group 5: officials. The referee (crew chief) is known on game day (prekick); before that
the crew counts as average. A crew's tendencies come only from its earlier games: penalties
per game and points per game against the league over the same span, shrunk toward zero
(`SHRINK` games of average), plus its home-minus-away penalty split."""

from __future__ import annotations

import pandas as pd

from betdata.nfl.store import AsOf

SHRINK = 16.0
SPAN_DAYS = 3 * 365


def _crews(gameday: pd.DataFrame, results: pd.DataFrame, tg: pd.DataFrame) -> dict[str, dict]:
    if results.empty or gameday.empty:
        return {}
    r = results.set_index("game_id")
    pen = tg.groupby(["game_id", "home"])["penalties"].sum().unstack(fill_value=0.0)
    d = gameday[["game_id", "referee"]].dropna().set_index("game_id").join(r, how="inner")
    d = d.join(pen, how="inner")
    if d.empty:
        return {}
    d["points"] = d.home_score + d.away_score
    d["pens"] = d.get(True, 0.0) + d.get(False, 0.0)
    d["home_minus_away"] = d.get(False, 0.0) - d.get(True, 0.0)  # more away penalties: +
    latest = r.known_at.max()
    d = d[d.known_at >= latest - pd.Timedelta(days=SPAN_DAYS)]
    lp, lpts, lha = d.pens.mean(), d.points.mean(), d.home_minus_away.mean()
    out = {}
    for ref, x in d.groupby("referee"):
        k = len(x) / (len(x) + SHRINK)
        out[ref] = {"pens": k * (x.pens.mean() - lp), "points": k * (x.points.mean() - lpts),
                    "home": k * (x.home_minus_away.mean() - lha)}  # fmt: skip
    return out


class Officials:
    name = "ref"
    version = 1

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        day = a.df("gameday")
        this = day.loc[day.game_id == g.game_id, "referee"]
        ref = this.iloc[-1] if len(this) and isinstance(this.iloc[-1], str) else None
        crews = a.cached("ref_crews", ("gameday", "results", "team_games"),
                         lambda: _crews(day, a.df("results"), a.df("team_games")))  # fmt: skip
        c = crews.get(ref, {}) if ref else {}
        return {
            "t_ref_pens": c.get("pens", 0.0),
            "t_ref_points": c.get("points", 0.0),
            "m_ref_home": c.get("home", 0.0),
        }
