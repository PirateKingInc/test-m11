"""M17 Part B: the engine's as-of rate estimates (MODEL.md "M17" Part B, A1 paddings).

Every estimate is an exponentially weighted sum over a unit's earlier games (half-life in
games; last season's games carried at `last_season_w`, older seasons dropped), read as of an
instant `t` (only games with `known_at <= t`), shrunk to a prior:

    rate = (sum_num + k * prior) / (sum_den + k),   k = A1 padding x `pad_mult`

Units: a team's offence, a team's defence (the opponents' offence against it), a QB, and a
skill player. The league prior is the mean over the last `league_window` team-games known at
`t`, so it follows the era. No betting line is read anywhere here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from betdata.nfl.engine import stability as S


@dataclass(frozen=True)
class RateParams:
    half_life: float = 6.0  # games
    last_season_w: float = 0.5
    pad_mult: float = 1.0
    league_window: int = 544  # team-games (two seasons)
    qb_weight: float = 0.5  # pass efficiency: weight of the QB's own rate vs the team's


TEAM_COLS = ["neutral_dropbacks", "neutral_plays", "neutral_secs", "neutral_gaps", "sacks",
             "scrambles", "dropbacks", "attempts", "runs", "plays", "pass_epa", "run_epa",
             "pass_yards", "run_yards", "scramble_yards", "sack_yards", "completions", "drives",
             "pass_td", "rush_td", "interceptions", "fumbles_lost"]  # fmt: skip
PLAYER_COLS = ["targets", "team_attempts", "receptions", "rec_yards", "air_yards", "yac",
               "rz_targets", "team_rz_attempts", "rec_td", "carries", "team_runs", "rush_yards",
               "rz_carries", "team_rz_runs", "rush_td", "games", "qb_dropbacks", "qb_sacks",
               "scrambles", "qb_attempts", "qb_epa", "scramble_yards_qb"]  # fmt: skip
# (estimate, numerator, denominator, A1 level, A1 rate) for teams
TEAM_RATES = {
    "neutral_pass": ("neutral_dropbacks", "neutral_plays", "team_off", "neutral_pass_rate"),
    "pace": ("neutral_secs", "neutral_gaps", "team_off", "pace_sec_per_play"),
    "sack": ("sacks", "dropbacks", "team_off", "sack_rate"),
    "scramble": ("scrambles", "dropbacks", "team_off", "scramble_rate"),
    "pass_epa": ("pass_epa", "dropbacks", "team_off", "pass_epa"),
    "run_epa": ("run_epa", "runs", "team_off", "run_epa"),
    "ypa": ("pass_yards", "attempts", "team_off", "yards_per_attempt"),
    "ypc": ("run_yards", "runs", "team_off", "yards_per_carry"),
    "comp": ("completions", "attempts", "qb", "completion_rate"),
    "pass_td_share": ("pass_td", "all_td", "team_off", "drive_td_rate"),
}  # fmt: skip
DEF_RATES = {
    "neutral_pass": ("neutral_dropbacks", "neutral_plays", "team_def", "neutral_pass_rate"),
    "sack": ("sacks", "dropbacks", "team_def", "sack_rate"),
    "pass_epa": ("pass_epa", "dropbacks", "team_def", "pass_epa"),
    "run_epa": ("run_epa", "runs", "team_def", "run_epa"),
    "ypa": ("pass_yards", "attempts", "team_def", "yards_per_attempt"),
    "ypc": ("run_yards", "runs", "team_def", "yards_per_carry"),
}  # fmt: skip
QB_RATES = {
    "sack": ("qb_sacks", "qb_dropbacks", "qb", "sack_rate"),
    "scramble": ("scrambles", "qb_dropbacks", "qb", "scramble_rate"),
    "epa": ("qb_epa", "qb_dropbacks", "qb", "epa_per_dropback"),
}  # fmt: skip
PLAYER_RATES = {
    "target_share": ("targets", "team_attempts", "receiver", "target_share"),
    "catch": ("receptions", "targets", "receiver", "catch_rate"),
    "ypt": ("rec_yards", "targets", "receiver", "yards_per_target"),
    "ypr": ("rec_yards", "receptions", "receiver", "yac_per_reception"),
    "rz_target_share": ("rz_targets", "team_rz_attempts", "receiver", "rz_target_share"),
    "carry_share": ("carries", "team_runs", "rusher", "carry_share"),
    "ypc": ("rush_yards", "carries", "rusher", "yards_per_carry"),
    "rz_carry_share": ("rz_carries", "team_rz_runs", "rusher", "rz_carry_share"),
}  # fmt: skip


def post_game_state(df: pd.DataFrame, cols: list[str], p: RateParams) -> pd.DataFrame:
    """Per row (a unit's game, sorted by kickoff), the weighted sums *after* that game, with
    the unit, season and known_at: the state any later instant reads."""
    df = df.sort_values("kickoff").reset_index(drop=True)
    decay = 0.5 ** (1 / p.half_life)
    vals = np.nan_to_num(df[cols].to_numpy(float))
    out = np.zeros_like(vals)
    seasons = df.season.to_numpy()
    for _, idx in df.groupby("unit", sort=False).indices.items():
        acc = np.zeros(len(cols))
        prev = None
        for i in idx:
            s = seasons[i]
            if prev is not None and s != prev:
                acc = acc * (p.last_season_w if s == prev + 1 else 0.0)
            acc = acc * decay + vals[i]
            out[i] = acc
            prev = s
    st = pd.DataFrame(out, columns=cols)
    st["unit"], st["season"] = df.unit.values, seasons
    st["known_at"] = pd.to_datetime(df.known_at.values, utc=True)
    return st


def read_state(state: pd.DataFrame, query: pd.DataFrame, cols: list[str],
               p: RateParams) -> pd.DataFrame:  # fmt: skip
    """The sums each (unit, t, season) query row sees: the unit's last state known at `t`,
    carried into the query's season (x last_season_w for last season, 0 if older)."""
    q = query.reset_index(drop=True).assign(_row=lambda x: np.arange(len(x)))
    q["t"] = pd.to_datetime(q.t, utc=True)
    q = q.sort_values("t")
    st = state.sort_values("known_at")
    m = pd.merge_asof(q, st.rename(columns={"season": "_s"}), left_on="t", right_on="known_at",
                      by="unit", allow_exact_matches=True, direction="backward")  # fmt: skip
    w = np.where(m._s.isna(), 0.0, np.where(m._s == m.season, 1.0,
                 np.where(m._s == m.season - 1, p.last_season_w, 0.0)))  # fmt: skip
    for c in cols:
        m[c] = np.nan_to_num(m[c].to_numpy(float)) * w
    return m.sort_values("_row").set_index(query.index)[cols]


def league_means(tg: pd.DataFrame, t: pd.Series, cols: list[str], window: int) -> pd.DataFrame:
    """Sums of `cols` over the last `window` team-games known at each `t` (ratios of these are
    the era's league rates)."""
    g = tg.sort_values("known_at")
    cs = np.vstack([np.zeros(len(cols)), np.cumsum(np.nan_to_num(g[cols].to_numpy(float)), 0)])
    k = np.searchsorted(g.known_at.to_numpy(), t.to_numpy(), side="right")
    lo = np.maximum(k - window, 0)
    return pd.DataFrame(cs[k] - cs[lo], columns=cols, index=t.index)


def _team_tables(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    tg = tables["team_games"].copy()
    pg = tables["player_games"]
    td = pg.groupby(["game_id", "team"])[["rec_td", "rush_td"]].sum().rename(
        columns={"rec_td": "pass_td"})
    tg = tg.merge(td, left_on=["game_id", "team"], right_index=True, how="left")
    tg[["pass_td", "rush_td"]] = tg[["pass_td", "rush_td"]].fillna(0)
    tg["all_td"] = tg.pass_td + tg.rush_td
    return tg


def _player_table(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    tg = tables["team_games"]
    pg = tables["player_games"].merge(
        tg[["game_id", "team", "attempts", "runs", "kickoff", "known_at"]].rename(
            columns={"attempts": "team_attempts", "runs": "team_runs"}),
        on=["game_id", "team"], how="left", suffixes=("", "_t"))  # fmt: skip
    rz = pg.groupby(["game_id", "team"])[["rz_targets", "rz_carries"]].sum().rename(
        columns={"rz_targets": "team_rz_attempts", "rz_carries": "team_rz_runs"})
    pg = pg.merge(rz, left_on=["game_id", "team"], right_index=True, how="left")
    pg["games"] = 1.0
    pg["scramble_yards_qb"] = 0.0
    if "kickoff_t" in pg:
        pg = pg.drop(columns=["kickoff_t", "known_at_t"])
    return pg


@dataclass
class RateStore:
    """Everything the engine estimates from, loaded once for a span of seasons."""

    tables: dict[str, pd.DataFrame]
    p: RateParams = field(default_factory=RateParams)
    # Training-season priors frozen in the fit file (live has no 2016-2019 tables): position
    # means and the league fallback rates; None = compute them from `tables`.
    priors: dict | None = None

    def __post_init__(self):
        tg = _team_tables(self.tables)
        self.team_games = tg
        self.off_state = post_game_state(tg.assign(unit=tg.team), TEAM_COLS + ["all_td"],
                                         self.p)  # fmt: skip
        self.def_state = post_game_state(tg.assign(unit=tg.opp), TEAM_COLS + ["all_td"], self.p)
        pl = _player_table(self.tables)
        self.players = pl
        self.player_state = post_game_state(pl.assign(unit=pl.player), PLAYER_COLS, self.p)
        self.pads = S.paddings()

    def _fallback(self, num: str, den: str) -> float:
        """The 2016-2019 league rate, for instants with no earlier team-games (early 2016)."""
        if self.priors is not None:
            return float(self.priors["fallback"].get(f"{num}/{den}", np.nan))
        g = self.team_games[self.team_games.season.between(2016, 2019)]
        return float(g[num].sum() / max(g[den].sum(), 1e-9))

    def _k(self, level: str, rate: str) -> float:
        row = self.pads.get((level, rate))
        k = row["padding"] if row and row["padding"] == row["padding"] else 400.0
        return float(k) * self.p.pad_mult

    def team(self, q: pd.DataFrame) -> pd.DataFrame:
        """q: rows with team, opp, season, t. Returns offence (`o_*`), defence-allowed (`d_*`)
        and league (`l_*`) rates per row."""
        cols = TEAM_COLS + ["all_td"]
        off = read_state(self.off_state, q.assign(unit=q.team), cols, self.p)
        de = read_state(self.def_state, q.assign(unit=q.opp), cols, self.p)
        lg = league_means(self.team_games, q.t, cols, self.p.league_window)
        out = pd.DataFrame(index=q.index)
        for name, (num, den, level, rate) in TEAM_RATES.items():
            league = (lg[num] / lg[den].replace(0, np.nan)).fillna(self._fallback(num, den))
            k = self._k(level, rate)
            out[f"l_{name}"] = league
            out[f"o_{name}"] = (off[num] + k * league) / (off[den] + k)
        for name, (num, den, level, rate) in DEF_RATES.items():
            league = (lg[num] / lg[den].replace(0, np.nan)).fillna(self._fallback(num, den))
            k = self._k(level, rate)
            out[f"d_{name}"] = (de[num] + k * league) / (de[den] + k)
        out["o_games"] = off.drives / np.maximum(lg.drives / max(self.p.league_window, 1), 1e-9)
        return out

    def player(self, q: pd.DataFrame, priors: pd.DataFrame | None = None) -> pd.DataFrame:
        """q: rows with player, season, t, position (and optional `prior_*` columns per row:
        a rookie's draft-round prior replaces the position mean). Returns shrunk player rates
        plus his weighted games of history (`games`)."""
        st = read_state(self.player_state, q.assign(unit=q.player), PLAYER_COLS, self.p)
        means = self.position_means()
        out = pd.DataFrame(index=q.index)
        for name, (num, den, level, rate) in PLAYER_RATES.items():
            k = self._k(level, rate)
            prior = q.position.map(means[name]).fillna(means[name].mean())
            if f"prior_{name}" in q:
                prior = q[f"prior_{name}"].fillna(prior)
            out[name] = (st[num] + k * prior) / (st[den] + k)
        for name, (num, den, level, rate) in QB_RATES.items():
            k = self._k(level, rate)
            prior = means[f"qb_{name}"]
            out[f"qb_{name}"] = (st[num] + k * prior) / (st[den] + k)
        out["games"] = st.games
        out["qb_dropbacks"] = st.qb_dropbacks
        return out

    def position_means(self) -> dict[str, pd.Series]:
        """Training-season (2016-2019) means by position: the prior of a player's rates."""
        if hasattr(self, "_means"):
            return self._means
        if self.priors is not None:
            self._means = {k: (pd.Series(v) if isinstance(v, dict) else float(v))
                           for k, v in self.priors["means"].items()}  # fmt: skip
            return self._means
        pl = self.players[self.players.season.between(2016, 2019)].copy()
        pl["position"] = pl.player.map(S.positions())
        means = {}
        for name, (num, den, _, _) in PLAYER_RATES.items():
            g = pl[pl[den] > 0].groupby("position")[[num, den]].sum()
            means[name] = (g[num] / g[den]).reindex(["QB", "RB", "WR", "TE", "FB"])
        qb = pl[(pl.position == "QB") & (pl.qb_dropbacks > 0)]
        for name, (num, den, _, _) in QB_RATES.items():
            means[f"qb_{name}"] = float(qb[num].sum() / qb[den].sum())
        self._means = means
        return means


def export_priors(store: RateStore) -> dict:
    """The training-season priors as plain JSON (for the frozen fit)."""
    means = {k: (v.dropna().to_dict() if isinstance(v, pd.Series) else float(v))
             for k, v in store.position_means().items()}  # fmt: skip
    pairs = {(num, den) for num, den, *_ in (*TEAM_RATES.values(), *DEF_RATES.values())}
    return {"means": means,
            "fallback": {f"{n}/{d}": store._fallback(n, d) for n, d in sorted(pairs)}}
