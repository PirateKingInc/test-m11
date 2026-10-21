"""NFL prop model (MODEL.md, M15 NFL player props).

Volume x share x rate, as the NBA prop model's minutes x per-minute rate:
  team volume   ridge per team and game for pass attempts, targets and carries on the team's
                last-6 and season averages, the opponent's pace, the side model's margin and
                total (game script), and wind.
  player terms  team volume x the player's share (last 6, season, EW; for targets also route
                participation x targets per route) x his rate (last 16 games, shrunk to the
                position's league rate), with teammates' missing share reassigned
                (share / (1 - missing)), the opponent's allowed rate by position, man/zone,
                weather (pass and receiving yards) and his own injury status as extra terms.
  stat ridge    one per market on those terms (plus his last-10 average).
Distributions: yards negative binomial or normal (chosen per market on validation, see
select.py), receptions negative binomial, touchdowns Poisson on the expected count
(P(anytime) = 1 - exp(-lambda)); dispersion fitted on training residuals.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import norm, poisson

from betdata.nba.props.model import Dispersion, fit_dispersion
from betdata.nfl.model import Ridge, fit_ridge

MARKETS = {"pass_yds": "pass_yds", "rush_yds": "rush_yds", "rec_yds": "rec_yds",
           "receptions": "rec", "anytime_td": "td"}  # fmt: skip
YARDS = ("pass_yds", "rush_yds", "rec_yds")
VOLUMES = {"att": "t_att", "tgt": "t_tgt", "car": "t_car"}
TEAM_FEATURES = ("tl6", "tsn", "opp_pace", "side_margin", "side_total", "wind")
LEAGUE_TOTAL = 44.0  # the side total is read relative to this; also its fill when missing
RATE_PRIOR = {"ypt": 40.0, "rpt": 40.0, "ypc": 40.0, "ypa": 100.0}
TERMS = {
    "rec_yds": ("b6", "bs", "be", "br", "bair", "redis", "opp", "wind", "mz", "own_q",
                "g10"),
    "receptions": ("b6", "bs", "be", "br", "redis", "opp", "wind", "own_q", "g10"),
    "rush_yds": ("b6", "bs", "be", "redis", "opp", "own_q", "g10"),
    "pass_yds": ("b6", "bs", "be", "mates", "opp", "wind", "own_q", "g10"),
    "anytime_td": ("rz6", "rzs", "rz_imp", "redis", "td6", "tds", "td16", "own_q"),
}


def populations(f: pd.DataFrame) -> dict[str, pd.Series]:
    """Who each market is scored on (as known before the game): starting-type roles."""
    ok = f.has_state.astype(bool)
    rcv = f.pos.isin(["WR", "TE", "RB"]) & (f.l6_tgt >= 2)
    return {"pass_yds": ok & (f.pos == "QB") & (f.l6_att >= 15),
            "rush_yds": ok & (f.l6_car >= 3), "rec_yds": ok & rcv, "receptions": ok & rcv,
            "anytime_td": ok & (f.pos != "QB") & ((f.l6_car + f.l6_tgt) >= 3)}  # fmt: skip


def _n(x, fill=0.0) -> np.ndarray:
    return np.nan_to_num(np.asarray(x, dtype=float), nan=fill)


def team_x(f: pd.DataFrame, vol: str) -> pd.DataFrame:
    c = VOLUMES[vol]
    total = _n(f.side_total, LEAGUE_TOTAL)
    return pd.DataFrame({"tl6": _n(f[f"tl6_{c}"], 33.0), "tsn": _n(f[f"tsn_{c}"], 33.0),
                         "opp_pace": _n(f.opp_tl6_dropbacks, 38.0) + _n(f.opp_tl6_t_car, 26.0),
                         "side_margin": _n(f.side_margin), "side_total": total,
                         "wind": _n(f.wind), "rain": _n(f.rain)}, index=f.index)  # fmt: skip


def rates(f: pd.DataFrame, league: dict[str, dict[str, float]]) -> pd.DataFrame:
    """Per-opportunity rates over the last 16 games, shrunk to the position's league rate."""
    pos = f.pos.fillna("WR")

    def shrunk(kind, num, den):
        lg = pos.map(league[kind]).fillna(np.mean(list(league[kind].values()))).to_numpy()
        return (_n(f[num]) + RATE_PRIOR[kind] * lg) / (_n(f[den]) + RATE_PRIOR[kind])

    out = pd.DataFrame({"ypt": shrunk("ypt", "s16_rec_yds", "s16_tgt"),
                        "rpt": shrunk("rpt", "s16_rec", "s16_tgt"),
                        "ypc": shrunk("ypc", "s16_rush_yds", "s16_car"),
                        "ypa": shrunk("ypa", "s16_pass_yds", "s16_att")}, index=f.index)
    return out


def league_rates(f: pd.DataFrame) -> dict[str, dict[str, float]]:
    out = {}
    for kind, num, den in (("ypt", "y_rec_yds", "y_tgt"), ("rpt", "y_rec", "y_tgt"),
                           ("ypc", "y_rush_yds", "y_car"), ("ypa", "y_pass_yds", "y_att")):
        s = f.groupby("pos")[[num, den]].sum()
        out[kind] = {p: float(r[num] / r[den]) for p, r in s.iterrows() if r[den] > 0}
    return out


def terms(f: pd.DataFrame, vol: dict[str, np.ndarray], league: dict, market: str,
          ) -> pd.DataFrame:  # fmt: skip
    r = rates(f, league)
    idx = f.index
    own_q = _n(f.own_q)
    if market == "anytime_td":
        rz6 = _n(f.l6_rz_tgt) + _n(f.l6_rz_car)
        mis = np.clip(0.5 * (_n(f.mis_tgt) + _n(f.mis_car)), 0, 0.8)
        imp = (_n(f.side_total, LEAGUE_TOTAL) / 2 + _n(f.side_margin) / 2) / (LEAGUE_TOTAL / 2)
        n16 = np.clip(_n(f.n_career, 1), 1, 16)
        return pd.DataFrame({"rz6": rz6, "rzs": _n(f.sn_rz_tgt) + _n(f.sn_rz_car),
                             "rz_imp": rz6 * (imp - 1), "redis": rz6 * mis / (1 - mis),
                             "td6": _n(f.l6_td), "tds": _n(f.sn_td),
                             "td16": _n(f.s16_td) / n16, "own_q": own_q * rz6}, index=idx)
    if market == "pass_yds":
        v, sh, rate, opp, mis = vol["att"], "att_share", r.ypa, f.opp_ypa, None
    elif market == "rush_yds":
        v, sh, rate, opp, mis = vol["car"], "car_share", r.ypc, f.opp_ypc, f.mis_car
    else:
        v, sh = vol["tgt"], "tgt_share"
        rate, opp = (r.ypt, f.opp_ypt) if market == "rec_yds" else (r.rpt, f.opp_rpt)
        mis = f.mis_tgt
    rate = _n(rate)
    b6, bs, be = (v * _n(f[f"{p}_{sh}"]) * rate for p in ("l6", "sn", "ew"))
    bm = 0.5 * (b6 + bs)
    out = {"b6": b6, "bs": bs, "be": be, "opp": bm * (_n(opp, 1.0) - 1),
           "wind": bm * _n(f.wind) / 10, "rain": bm * _n(f.rain), "own_q": bm * own_q,
           "g10": _n(f[f"g10_{MARKETS[market]}"])}  # fmt: skip
    if mis is not None:
        m = np.clip(_n(mis), 0, 0.8)
        out["redis"] = bm * m / (1 - m)
    if market == "pass_yds":
        out["mates"] = bm * np.clip(_n(f.mis_tgt), 0, 0.8)
    if market in ("rec_yds", "receptions"):
        tprr = _n(f.s16_tgt) / np.maximum(_n(f.s16_routes_f), 1.0)
        role_share = _n(f.l6_route_f) * tprr * _n(f.tl6_dropbacks, 38) / np.maximum(
            _n(f.tl6_t_tgt, 33), 1.0)  # fmt: skip
        out["br"] = v * role_share * rate
        out["bair"] = v * _n(f.l6_air_share) * 8.0
        out["mz"] = _mz(f, v)
    return pd.DataFrame(out, index=idx)


def _mz(f: pd.DataFrame, vol_tgt: np.ndarray, prior: float = 150.0) -> np.ndarray:
    """Receiving yards from the opponent's man rate (vs the league) x the player's own man vs
    zone yards per route (last 16 games, each shrunk to his overall rate)."""
    rm, rz = _n(f.s16_routes_man), _n(f.s16_routes_zone)
    ym, yz = _n(f.s16_yds_man), _n(f.s16_yds_zone)
    overall = (ym + yz) / np.maximum(rm + rz, 1.0)
    d = (ym + prior * overall) / (rm + prior) - (yz + prior * overall) / (rz + prior)
    routes = _n(f.l6_route_f) * _n(f.tl6_dropbacks, 38)
    return routes * _n(f.opp_man_diff) * d


@dataclass
class YardsDist:
    """Normal (continuity-corrected, floored at 0: only P(over) at lines >= 0 is used) or
    negative binomial on yards, variance a + b*mean + c*mean^2."""

    family: str
    disp: Dispersion

    def sd(self, mu):
        return np.sqrt(self.disp.var(np.maximum(np.asarray(mu, dtype=float), 0.05)))

    def p_over(self, mu, line):
        if self.family == "negbin":
            return self.disp.p_over(mu, line)
        mu = np.maximum(np.asarray(mu, dtype=float), 0.05)
        k = np.floor(np.asarray(line, dtype=float))
        return 1 - norm.cdf((k + 0.5 - mu) / self.sd(mu))

    def p_push(self, mu, line):
        if self.family == "negbin":
            return self.disp.p_push(mu, line)
        mu = np.maximum(np.asarray(mu, dtype=float), 0.05)
        line = np.asarray(line, dtype=float)
        s = self.sd(mu)
        pm = norm.cdf((line + 0.5 - mu) / s) - norm.cdf((line - 0.5 - mu) / s)
        return np.where(np.isclose(line, np.round(line)), pm, 0.0)

    def to_dict(self) -> dict:
        return {**self.disp.to_dict(), "family": self.family}


def make_dist(d: dict):
    disp = Dispersion(d["a"], d["b"], d["c"])
    return disp if d["family"] == "negbin_count" else YardsDist(d["family"], disp)


class CountDist(Dispersion):
    def to_dict(self) -> dict:
        return {**super().to_dict(), "family": "negbin_count"}


@dataclass
class PropModel:
    team: dict[str, Ridge]
    league: dict[str, dict[str, float]]
    stats: dict[str, Ridge]
    dists: dict[str, object]  # YardsDist for yards, CountDist for receptions; none for TDs
    calib: dict[str, tuple[float, float]] = field(default_factory=dict)

    def volumes(self, f: pd.DataFrame) -> dict[str, np.ndarray]:
        out = {}
        for v, r in self.team.items():
            X = team_x(f, v)[list(r.names)].to_numpy(dtype=float)
            out[v] = np.maximum(r.predict(X), 1.0)
        return out

    def terms(self, f: pd.DataFrame, market: str, vol=None) -> pd.DataFrame:
        return terms(f, vol or self.volumes(f), self.league, market)

    def predict(self, f: pd.DataFrame, market: str, vol=None) -> np.ndarray:
        X = self.terms(f, market, vol)[list(self.stats[market].names)].to_numpy(dtype=float)
        lo = 0.01 if market == "anytime_td" else 0.0
        return np.maximum(self.stats[market].predict(X), lo)

    def raw_p_over(self, market: str, mu, line) -> np.ndarray:
        if market == "anytime_td":  # P(at least one): the line is 0.5 (or none)
            return 1 - poisson.pmf(0, np.maximum(np.asarray(mu, dtype=float), 0.01))
        return self.dists[market].p_over(mu, line)

    def p_over(self, market: str, mu, line) -> np.ndarray:
        p = self.raw_p_over(market, mu, line)
        if market in self.calib:
            a, b = self.calib[market]
            q = np.clip(p, 1e-6, 1 - 1e-6)
            p = 1 / (1 + np.exp(-(a * np.log(q / (1 - q)) + b)))
        return p

    def p_push(self, market: str, mu, line) -> np.ndarray:
        if market == "anytime_td":
            return np.zeros(np.shape(np.asarray(mu, dtype=float)))
        return self.dists[market].p_push(mu, line)

    def to_dict(self) -> dict:
        return {"team": {k: v.to_dict() for k, v in self.team.items()}, "league": self.league,
                "stats": {k: v.to_dict() for k, v in self.stats.items()},
                "dists": {k: v.to_dict() for k, v in self.dists.items()},
                "calib": {k: list(v) for k, v in self.calib.items()}}  # fmt: skip

    @classmethod
    def from_dict(cls, d: dict) -> PropModel:
        return cls({k: Ridge.from_dict(v) for k, v in d["team"].items()}, d["league"],
                   {k: Ridge.from_dict(v) for k, v in d["stats"].items()},
                   {k: make_dist(v) for k, v in d["dists"].items()},
                   {k: tuple(v) for k, v in d.get("calib", {}).items()})  # fmt: skip


def fit(train: pd.DataFrame, families: dict[str, str] | None = None, lam_team: float = 10.0,
        lam_stat: float | dict = 10.0, term_sets: dict | None = None) -> PropModel:  # fmt: skip
    """`train`: features of played player-games with the actuals (y_*)."""
    families = families or {}
    term_sets = {**TERMS, **(term_sets or {})}
    lams = lam_stat if isinstance(lam_stat, dict) else {}
    lam0 = 10.0 if isinstance(lam_stat, dict) else lam_stat
    league = league_rates(train)
    team = {}
    tk = train.drop_duplicates(["game_id", "team_id", "point"])
    sums = tk.set_index(["game_id", "team_id", "point"])[["yt_att", "yt_tgt", "yt_car"]]
    k = pd.MultiIndex.from_frame(tk[["game_id", "team_id", "point"]])
    for v in VOLUMES:
        X = team_x(tk, v)[list(TEAM_FEATURES)].to_numpy(dtype=float)
        team[v] = fit_ridge(X, sums[f"yt_{v}"].reindex(k).to_numpy(), TEAM_FEATURES, lam_team)
    mod = PropModel(team, league, {}, {})
    pops = populations(train)
    for market, stat in MARKETS.items():
        d = train[pops[market]]
        vol = mod.volumes(d)
        names = term_sets[market]
        X = terms(d, vol, league, market)[list(names)].to_numpy(dtype=float)
        y = np.maximum(d[f"y_{stat}"].to_numpy(dtype=float), 0.0)
        r = fit_ridge(X, y, names, lams.get(market, lam0))
        mod.stats[market] = r
        if market == "anytime_td":
            continue
        disp = fit_dispersion(np.maximum(r.predict(X), 0.05), y)
        if market == "receptions":
            mod.dists[market] = CountDist(disp.a, disp.b, disp.c)
        else:
            mod.dists[market] = YardsDist(families.get(market, "negbin"), disp)
    return mod
