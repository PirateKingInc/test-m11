"""Group 7: game context. Division games (schedule). Late-season stakes from the standings
known at the prediction point: from week 12, teams near .500 are in the playoff race and teams
far below it are out of it; in the last regular-season week, teams with a clearly winning
record (likely to have clinched) may rest starters."""

from __future__ import annotations

from betdata.nfl.store import AsOf

RACE_WEEK = 12


def _records(results, games) -> dict[tuple[int, int], tuple[int, int]]:
    """(season, team) -> (wins, games) over regular-season results known."""
    out: dict[tuple[int, int], list[float]] = {}
    r = results.join(games[["season", "season_type", "home", "away"]], on="game_id")
    r = r[r.season_type == "REG"]
    for x in r.itertuples(index=False):
        tie = x.home_score == x.away_score
        for team, won in ((x.home, x.home_score > x.away_score),
                          (x.away, x.away_score > x.home_score)):  # fmt: skip
            rec = out.setdefault((x.season, team), [0.0, 0])
            rec[0] += 1.0 if won else 0.5 if tie else 0.0
            rec[1] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


class GameContext:
    name = "ctx"
    version = 1

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        last_week = a.store.cache.get("ctx_last_week")
        if last_week is None:  # schedule fact
            reg = a.games[a.games.season_type == "REG"]
            last_week = a.store.cache["ctx_last_week"] = reg.groupby("season")["week"].max()
        recs = a.cached("ctx_records", ("results",), lambda: _records(a.df("results"), a.games))
        div = float(bool(g.div_game)) if g.div_game is not None else 0.0
        vals = {}
        late = g.season_type == "REG" and g.week >= RACE_WEEK
        final_week = g.season_type == "REG" and g.week == last_week.get(g.season, -1)
        for side, team in (("h", g.home), ("w", g.away)):
            wins, n = recs.get((g.season, team), (0.0, 0))
            pct = wins / n if n else 0.5
            vals[side] = {
                "race": float(late and 0.4 <= pct <= 0.65),
                "out": float(late and pct < 0.35),
                "rest": float(final_week and pct >= 0.75),
            }
        out = {"m_ctx_div": div * (0.0 if g.neutral else 1.0), "t_ctx_div": div}
        for k in vals["h"]:
            out[f"m_ctx_{k}"] = vals["h"][k] - vals["w"][k]
            out[f"t_ctx_{k}"] = vals["h"][k] + vals["w"][k]
        return out
