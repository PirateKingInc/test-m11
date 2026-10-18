"""Unit model: pass/run offense and defense ratings, adjusted for missing starters.

Each team carries four ratings in expected points added (EPA) per play, relative to the
league: pass offense, run offense, pass defense, run defense (a defense rating is what it
allows, so higher is worse). After each game, the gap between what an offense gained and
what was expected (league + offense + opposing defense) moves both units, like Elo.

A game then has four matchups (each offense against the other side's defense). On top of
them sit the missing starters per position group (see context.py), including:
  * a cluster term: two or three defensive backs out hurt more than the sum of one each;
  * an interaction: a depleted secondary matters more against a strong passing offense.
How many points each of these is worth is learned by regression on training seasons only,
separately for the margin and for total points.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from betdata.analysis.backtest import EASTERN
from betdata.analysis.context import (
    DEFAULT_OUT_PROB,
    GROUPS,
    Absence,
    Context,
    StarterTracker,
    absences,
    missing_by_group,
)
from betdata.analysis.dataset import GameRow

TYPICAL_PASS_PLAYS = 36.0
TYPICAL_RUSH_PLAYS = 26.0


@dataclass
class UnitRatings:
    alpha: float = 0.1  # how far one game moves a unit rating
    revert: float = 0.4  # share of each rating pulled back to average between seasons
    league_alpha: float = 0.01
    mu_pass: float = 0.0
    mu_rush: float = -0.05
    po: dict[int, float] = field(default_factory=dict)
    ro: dict[int, float] = field(default_factory=dict)
    pd: dict[int, float] = field(default_factory=dict)
    rd: dict[int, float] = field(default_factory=dict)
    season: int | None = None

    def roll(self, season: int) -> None:
        if self.season is not None and season != self.season:
            for d in (self.po, self.ro, self.pd, self.rd):
                for t in d:
                    d[t] *= 1 - self.revert
        self.season = season

    def pass_edge(self, off: int, dfn: int) -> float:
        """Expected pass EPA/play above league average for `off` against `dfn`."""
        return self.po.get(off, 0.0) + self.pd.get(dfn, 0.0)

    def rush_edge(self, off: int, dfn: int) -> float:
        return self.ro.get(off, 0.0) + self.rd.get(dfn, 0.0)

    def update(self, off: int, dfn: int, stats: tuple[int, float, int, float]) -> None:
        pass_plays, pass_epa, rush_plays, rush_epa = stats
        if pass_plays > 0:
            obs = pass_epa / pass_plays
            resid = obs - (self.mu_pass + self.pass_edge(off, dfn))
            w = self.alpha * min(pass_plays / TYPICAL_PASS_PLAYS, 1.5)
            self.po[off] = self.po.get(off, 0.0) + w * resid
            self.pd[dfn] = self.pd.get(dfn, 0.0) + w * resid
            self.mu_pass += self.league_alpha * (obs - self.mu_pass)
        if rush_plays > 0:
            obs = rush_epa / rush_plays
            resid = obs - (self.mu_rush + self.rush_edge(off, dfn))
            w = self.alpha * min(rush_plays / TYPICAL_RUSH_PLAYS, 1.5)
            self.ro[off] = self.ro.get(off, 0.0) + w * resid
            self.rd[dfn] = self.rd.get(dfn, 0.0) + w * resid
            self.mu_rush += self.league_alpha * (obs - self.mu_rush)


# --- Features ----------------------------------------------------------------------------

UNIT_MARGIN = ("hfa", "pass", "rush")
UNIT_TOTAL = ("league_total", "pass_sum", "rush_sum")
INJURY_MARGIN = tuple(f"miss_{g}" for g in GROUPS) + ("secondary_sq", "ol_sq", "secondary_x_pass")
INJURY_TOTAL = tuple(f"miss_{g}_sum" for g in GROUPS) + (
    "secondary_sq_sum", "ol_sq_sum", "secondary_x_pass_sum",
)  # fmt: skip


@dataclass
class GameFeatures:
    game: GameRow
    margin: dict[str, float]
    total: dict[str, float]
    home_out: list[Absence]
    away_out: list[Absence]
    has_snaps: bool  # starters were known for both teams


def build_features(
    games: Sequence[GameRow],
    ctx: Context,
    *,
    alpha: float = 0.1,
    revert: float = 0.4,
    out_prob: dict[str, float] = DEFAULT_OUT_PROB,
    played_log: list[tuple[str, bool, int]] | None = None,
) -> list[GameFeatures]:
    """Walk forward a day at a time: features for every game that day from what was known
    before it, then learn from that day's finished games. With `played_log`, also records
    (report status, actually played, season) for listed starters, to estimate how often a
    Questionable or Doubtful starter sits out."""
    ratings = UnitRatings(alpha=alpha, revert=revert)
    tracker = StarterTracker(ctx)
    league_total = 43.0
    out: list[GameFeatures] = []
    ordered = sorted(games, key=lambda g: (g.start, g.game_id))
    for _, day in itertools.groupby(ordered, key=lambda g: g.start.astimezone(EASTERN).date()):
        day = list(day)
        for g in day:
            ratings.roll(g.season)
            h, a = g.home_team_id, g.away_team_id
            week = g.week or 0
            hs, as_ = tracker.starters(g.season, h), tracker.starters(g.season, a)
            h_out = absences(ctx, hs, g.season, week, h, out_prob)
            a_out = absences(ctx, as_, g.season, week, a, out_prob)
            mh, ma = missing_by_group(h_out), missing_by_group(a_out)
            sec_h, sec_a = mh["CB"] + mh["S"], ma["CB"] + ma["S"]
            pass_h, pass_a = ratings.pass_edge(h, a), ratings.pass_edge(a, h)
            rush_h, rush_a = ratings.rush_edge(h, a), ratings.rush_edge(a, h)
            po_h, po_a = ratings.po.get(h, 0.0), ratings.po.get(a, 0.0)
            margin = {
                "hfa": 0.0 if g.neutral else 1.0,
                "pass": pass_h - pass_a,
                "rush": rush_h - rush_a,
                **{f"miss_{grp}": ma[grp] - mh[grp] for grp in GROUPS},
                "secondary_sq": sec_a**2 - sec_h**2,
                "ol_sq": ma["OL"] ** 2 - mh["OL"] ** 2,
                "secondary_x_pass": sec_a * po_h - sec_h * po_a,
            }
            total = {
                "league_total": league_total,
                "pass_sum": pass_h + pass_a,
                "rush_sum": rush_h + rush_a,
                **{f"miss_{grp}_sum": ma[grp] + mh[grp] for grp in GROUPS},
                "secondary_sq_sum": sec_a**2 + sec_h**2,
                "ol_sq_sum": ma["OL"] ** 2 + mh["OL"] ** 2,
                "secondary_x_pass_sum": sec_a * po_h + sec_h * po_a,
            }
            out.append(GameFeatures(g, margin, total, h_out, a_out, bool(hs and as_)))
            if played_log is not None:
                _log_played(ctx, g, h, hs, played_log)
                _log_played(ctx, g, a, as_, played_log)
        for g in day:
            stats = ctx.stats.get(g.game_id, {})
            h, a = g.home_team_id, g.away_team_id
            if h in stats:
                ratings.update(h, a, stats[h])
            if a in stats:
                ratings.update(a, h, stats[a])
            tracker.record(g.game_id, g.season, h)
            tracker.record(g.game_id, g.season, a)
            if g.final:
                league_total += 0.01 * (g.home_score + g.away_score - league_total)
    return out


def _log_played(ctx: Context, g: GameRow, team_id: int, starters, log) -> None:
    report = ctx.reports.get((g.season, g.week or 0, team_id), {})
    played = {k for k, *_ in ctx.snaps.get(g.game_id, {}).get(team_id, [])}
    if not played:
        return
    for s in starters:
        status = report.get(s.key)
        if status in ("Questionable", "Doubtful", "Out"):
            log.append((status, s.key in played, g.season))


def estimate_out_prob(log: list[tuple[str, bool, int]], seasons: set[int]) -> dict[str, float]:
    """Share of listed starters who did not play, per report status, on the given seasons."""
    probs = dict(DEFAULT_OUT_PROB)
    for status in ("Questionable", "Doubtful"):
        rows = [played for st, played, season in log if st == status and season in seasons]
        if len(rows) >= 50:
            probs[status] = round(1 - sum(rows) / len(rows), 3)
    return probs


# --- Regression --------------------------------------------------------------------------


@dataclass
class LinearFit:
    names: tuple[str, ...]
    means: list[float]
    sds: list[float]
    coef: list[float]  # on standardised features
    intercept: float

    def predict(self, x: dict[str, float]) -> float:
        return self.intercept + sum(
            c * (x[n] - m) / s
            for n, m, s, c in zip(self.names, self.means, self.sds, self.coef, strict=True)
        )

    def raw_coef(self, name: str) -> float:
        """Effect of one unit of feature `name`, in points."""
        i = self.names.index(name)
        return self.coef[i] / self.sds[i]


def ridge(
    rows: list[dict[str, float]], y: list[float], names: Sequence[str], lam: float = 2.0
) -> LinearFit:
    """Ridge regression on standardised features (pure Python; a handful of features)."""
    n, k = len(rows), len(names)
    means = [sum(r[c] for r in rows) / n for c in names]
    sds = []
    for j, c in enumerate(names):
        var = sum((r[c] - means[j]) ** 2 for r in rows) / n
        sds.append(math.sqrt(var) if var > 1e-12 else 1.0)
    z = [[(r[c] - means[j]) / sds[j] for j, c in enumerate(names)] for r in rows]
    y_mean = sum(y) / n
    yc = [v - y_mean for v in y]
    a = [
        [sum(zi[p] * zi[q] for zi in z) + (lam if p == q else 0.0) for q in range(k)]
        for p in range(k)
    ]
    b = [sum(zi[p] * yi for zi, yi in zip(z, yc, strict=True)) for p in range(k)]
    coef = _solve(a, b)
    return LinearFit(tuple(names), means, sds, coef, y_mean)


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    k = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(k):
        piv = max(range(col, k), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        if abs(m[col][col]) < 1e-12:
            raise ValueError("singular system")
        for r in range(k):
            if r != col:
                f = m[r][col] / m[col][col]
                if f:
                    m[r] = [x - f * yv for x, yv in zip(m[r], m[col], strict=True)]
    return [m[i][k] / m[i][i] for i in range(k)]


@dataclass
class UnitModelFit:
    alpha: float
    revert: float
    out_prob: dict[str, float]
    injuries: bool
    margin: LinearFit
    total: LinearFit

    @property
    def version(self) -> str:
        tag = "units+missing" if self.injuries else "units"
        q, d = self.out_prob["Questionable"], self.out_prob["Doubtful"]
        return f"{tag}:alpha={self.alpha:g},revert={self.revert:g},q={q:g},d={d:g}"


def feature_names(injuries: bool) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if injuries:
        return UNIT_MARGIN + INJURY_MARGIN, UNIT_TOTAL + INJURY_TOTAL
    return UNIT_MARGIN, UNIT_TOTAL


def fit(
    feats: Sequence[GameFeatures], train: set[int], *, alpha, revert, out_prob, injuries
) -> UnitModelFit:
    rows = [f for f in feats if f.game.final and f.game.season in train and f.has_snaps]
    if len(rows) < 100:
        raise ValueError(f"only {len(rows)} training games with team stats and snap counts")
    m_names, t_names = feature_names(injuries)
    margin = ridge([f.margin for f in rows], [f.game.home_margin for f in rows], m_names)
    total = ridge(
        [f.total for f in rows], [f.game.home_score + f.game.away_score for f in rows], t_names
    )
    return UnitModelFit(alpha, revert, out_prob, injuries, margin, total)
