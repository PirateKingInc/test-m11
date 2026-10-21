"""The NBA prop model's player-game panel, as of each prediction point (MODEL.md, M15).

Everything about a player-game comes from three time-stamped sources:
  box        each finished game's box line, known at `known_at` (tip + 3 h for history, the
             fetch time live). A player's *state* after a game (recent minutes, per-minute
             rates, starts, fouls, role) is computed over his played games up to and including
             it, and is read for a later game only if known by that game's prediction point.
  injuries   the official report, the last status known by the point (M14 play probabilities).
  side       the M14 side model's margin and total for the game at the point.

A target is (game, team, player, point). For history the targets are the players who played;
live, the players with a stored prop line.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import Connection, select

from betdata.db.schema import games, nba_player_games
from betdata.nba.data import _utc, load_games
from betdata.nba.features import DEFAULT_PLAY, status_key
from betdata.nba.points import POINTS, point_time

STATS = ("pts", "reb", "ast", "fg3m", "pra", "fg3a", "fga", "fta", "tov")
MARKETS = {"points": "pts", "rebounds": "reb", "assists": "ast", "threes": "fg3m", "pra": "pra"}
ROLES = ("G", "F", "C")
ROTATION_GAMES = 10  # a team's rotation: who played for it in its last 10 games
RETURN_GAP_DAYS = 8  # a gap this long within a season marks a return from an absence
OPP_GAMES = 20
OPP_PRIOR_MIN = 600.0  # opponent allowances are shrunk with this many league-average minutes
LEAGUE_ROWS = 1200  # league role rates: the last ~1,200 (game, team) rows, about 40 days


def load_box(conn: Connection, min_season: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(games, box rows with game context). Box rows include did-not-play lines."""
    g = load_games(conn, min_season)
    t = nba_player_games
    p = pd.read_sql(
        select(t.c.game_id, t.c.team_id, t.c.player_id, t.c.player_name, t.c.starter,
               t.c.minutes, t.c.points, t.c.fg3m, t.c.fg3a, t.c.fga, t.c.fta, t.c.oreb,
               t.c.dreb, t.c.ast, t.c.tov, t.c.pf, t.c.known_at)
        .join(games, games.c.game_id == t.c.game_id)
        .where(games.c.season >= (min_season or 0)), conn)  # fmt: skip
    p = p[p.game_id.isin(g.index)].copy()
    num = ["minutes", "points", "fg3m", "fg3a", "fga", "fta", "oreb", "dreb", "ast", "tov", "pf"]
    p[num] = p[num].astype(float).fillna(0.0)
    p["starter"] = p["starter"].fillna(False).astype(bool)
    p["known_at"] = _utc(p["known_at"])
    p["pts"] = p.pop("points")
    p["reb"] = p.oreb + p.dreb
    p["pra"] = p.pts + p.reb + p.ast
    p["season"] = p.game_id.map(g.season)
    p["tip"] = p.game_id.map(g.tip)
    home = p.game_id.map(g.home)
    p["is_home"] = p.team_id == home
    p["opp_id"] = np.where(p.is_home, p.game_id.map(g.away), home)
    p["played"] = p.minutes > 0
    return g, p.sort_values(["tip", "game_id", "team_id", "player_id"]).reset_index(drop=True)


def team_schedule(g: pd.DataFrame) -> pd.DataFrame:
    """One row per (team, game) with the team's game index, rest days and back-to-back."""
    s = pd.concat([g[["game_id", "tip", "season", "home"]].rename(columns={"home": "team_id"}),
                   g[["game_id", "tip", "season", "away"]].rename(columns={"away": "team_id"})])
    s = s.sort_values(["team_id", "tip"]).reset_index(drop=True)
    s["idx"] = s.groupby("team_id").cumcount()
    prev = s.groupby("team_id").tip.shift(1)
    same = s.groupby("team_id").season.shift(1) == s.season
    days = (s.tip - prev).dt.total_seconds() / 86400
    s["rest"] = np.where(same, np.clip(np.floor(days + 0.25) - 1, 0, 4), 4.0)
    s["b2b"] = (same & (days < 1.6)).astype(float)
    return s


def player_states(p: pd.DataFrame, sched: pd.DataFrame) -> pd.DataFrame:
    """A player's state after each of his played games: what a later prediction may read once
    the game's box score is known (`known_at`)."""
    d = p[p.played].sort_values(["player_id", "tip"]).copy()
    by = d.groupby("player_id", sort=False)
    out = d[["player_id", "player_name", "team_id", "game_id", "season", "tip", "known_at"]].copy()
    for k in (3, 10, 30):
        out[f"min_l{k}"] = by.minutes.transform(lambda s, k=k: s.rolling(k, 1).mean())
    out["min_ewm"] = by.minutes.transform(lambda s: s.ewm(halflife=5).mean())
    msum10 = by.minutes.transform(lambda s: s.rolling(10, 1).sum())
    msum30 = by.minutes.transform(lambda s: s.rolling(30, 1).sum())
    for st in STATS:
        out[f"r10_{st}"] = by[st].transform(lambda s: s.rolling(10, 1).sum()) / msum10
        out[f"r30_{st}"] = by[st].transform(lambda s: s.rolling(30, 1).sum()) / msum30
        out[f"g10_{st}"] = by[st].transform(lambda s: s.rolling(10, 1).mean())
    out["start_l10"] = by.starter.transform(lambda s: s.astype(float).rolling(10, 1).mean())
    out["pf_rate"] = by.pf.transform(lambda s: s.rolling(30, 1).sum()) / msum30
    out["n_season"] = d.groupby(["player_id", "season"]).cumcount() + 1
    out["n_career"] = by.cumcount() + 1
    # Games with this team (a new arrival's integration) and played games since a return.
    stint = (d.team_id != by.team_id.shift(1)).groupby(d.player_id).cumsum()
    out["n_team"] = d.groupby([d.player_id, stint]).cumcount() + 1
    gap = (d.tip - by.tip.shift(1)).dt.total_seconds() / 86400
    ret = (gap >= RETURN_GAP_DAYS) & (by.season.shift(1) == d.season)
    ret_id = ret.groupby(d.player_id).cumsum()
    out["since_return"] = np.where(ret_id > 0, d.groupby([d.player_id, ret_id]).cumcount(), 99)
    idx = sched.set_index(["team_id", "game_id"]).idx
    out["team_idx"] = idx.reindex(pd.MultiIndex.from_frame(d[["team_id", "game_id"]])).to_numpy()
    ast36, reb36, tpa36 = (36 * out.r30_ast, 36 * out.r30_reb, 36 * out.r30_fg3a)
    out["role"] = np.where(ast36 >= 5.0, "G", np.where((reb36 >= 9.0) & (tpa36 < 3.0), "C", "F"))
    return out.reset_index(drop=True)


def asof_states(states: pd.DataFrame, keys: pd.DataFrame, at: str = "t") -> pd.DataFrame:
    """For each row of `keys` (player_id, `at`), the player's latest state known by `at`."""
    k = keys.reset_index().rename(columns={"index": "_row"}).sort_values(at)
    s = states.sort_values("known_at")
    m = pd.merge_asof(k, s.drop(columns=["player_name"]), left_on=at, right_on="known_at",
                      by="player_id", allow_exact_matches=True, suffixes=("", "_s"))
    return m.sort_values("_row").set_index("_row")


def injuries_asof(inj: pd.DataFrame, keys: pd.DataFrame) -> pd.DataFrame:
    """Status known by `t` for (game_id, player_id), and whether the team was on any report
    for the game by then."""
    i = inj.dropna(subset=["player_id"]).sort_values("known_at")
    k = keys.reset_index().rename(columns={"index": "_row"}).sort_values("t")
    st = pd.merge_asof(k, i[["game_id", "player_id", "status", "known_at"]], left_on="t",
                       right_on="known_at", by=["game_id", "player_id"])  # fmt: skip
    rep = inj.sort_values("known_at")[["game_id", "team_id", "known_at"]]
    rp = pd.merge_asof(k[["_row", "t", "game_id", "team_id"]], rep, left_on="t",
                       right_on="known_at", by=["game_id", "team_id"])  # fmt: skip
    out = st.set_index("_row")[["status"]]
    out["reported"] = rp.set_index("_row").known_at.notna()
    return out.sort_index()


def play_probs(status: pd.Series, streak: pd.Series, reported: pd.Series,
               table: dict[str, float]) -> np.ndarray:  # fmt: skip
    def bucket(n: float) -> str:
        return "0" if n <= 0 else "1" if n == 1 else "2-4" if n <= 4 else "5+"

    out = []
    for s, n, r in zip(status, streak, reported, strict=True):
        key = status_key(s if isinstance(s, str) else None, int(n), bool(r))
        out.append(table.get(key, DEFAULT_PLAY.get(key, {"0": 0.97, "1": 0.6, "2-4": 0.35,
                                                         "5+": 0.12}[bucket(n)])))  # fmt: skip
    return np.asarray(out, dtype=float)


def point_times(g: pd.DataFrame, game_ids, points=POINTS) -> pd.DataFrame:
    rows = [(gid, pt, point_time(pt, g.at[gid, "tip"])) for gid in game_ids for pt in points]
    out = pd.DataFrame(rows, columns=["game_id", "point", "t"])
    out["t"] = _utc(out["t"])
    return out


def opponent_allowances(p: pd.DataFrame, states: pd.DataFrame) -> pd.DataFrame:
    """Per (game, defending team): each role's stats and minutes against it in that game, the
    role taken from the player's state before the game; plus the league's running role rates."""
    d = p[p.played][["game_id", "tip", "known_at", "player_id", "opp_id", "minutes",
                     *STATS]].copy()  # fmt: skip
    keys = d[["player_id"]].assign(t=d.tip - pd.Timedelta(seconds=1))
    d["role"] = asof_states(states, keys)["role"].fillna("F").to_numpy()
    agg = d.groupby(["game_id", "opp_id", "role"]).agg(
        tip=("tip", "first"), known_at=("known_at", "max"), minutes=("minutes", "sum"),
        **{s: (s, "sum") for s in STATS})  # fmt: skip
    return agg.reset_index().rename(columns={"opp_id": "def_team"})


def _opp_factors(allow: pd.DataFrame, keys: pd.DataFrame) -> pd.DataFrame:
    """For each key (def_team, role, t): the defending team's allowed per-minute rates for the
    role over its last OPP_GAMES games known by t, relative to the league's role rate over all
    games known by t, shrunk toward 1."""
    a = allow.sort_values("known_at")
    roll = a.groupby(["def_team", "role"], sort=False)
    r = a[["def_team", "role", "known_at"]].copy()
    r["min_sum"] = roll.minutes.transform(lambda s: s.rolling(OPP_GAMES, 1).sum())
    lg = a[["role", "known_at"]].copy()
    # The league's role rates over its recent games (the three-point rate keeps rising).
    lg_roll = a.groupby("role", sort=False)
    lg["lmin"] = lg_roll.minutes.transform(lambda x: x.rolling(LEAGUE_ROWS, 1).sum())
    for s in ("pts", "reb", "ast", "fg3m", "pra"):
        r[s] = roll[s].transform(lambda x: x.rolling(OPP_GAMES, 1).sum())
        lg[f"l_{s}"] = lg_roll[s].transform(lambda x: x.rolling(LEAGUE_ROWS, 1).sum())
    k = keys.reset_index().rename(columns={"index": "_row"}).sort_values("t")
    m = pd.merge_asof(k, r.sort_values("known_at"), left_on="t", right_on="known_at",
                      by=["def_team", "role"])  # fmt: skip
    m = pd.merge_asof(m.sort_values("t"), lg.sort_values("known_at"), left_on="t",
                      right_on="known_at", by="role", suffixes=("", "_l"))  # fmt: skip
    out = pd.DataFrame(index=m._row)
    for s in ("pts", "reb", "ast", "fg3m", "pra"):
        league = (m[f"l_{s}"] / m.lmin).to_numpy()
        allowed = ((m[s].fillna(0) + OPP_PRIOR_MIN * league)
                   / (m.min_sum.fillna(0) + OPP_PRIOR_MIN)).to_numpy()  # fmt: skip
        with np.errstate(divide="ignore", invalid="ignore"):
            out[f"opp_{s}"] = np.where(league > 0, allowed / league, 1.0)
    return out.sort_index().fillna(1.0)


def build_features(g: pd.DataFrame, p: pd.DataFrame, states: pd.DataFrame, inj: pd.DataFrame,
                   allow: pd.DataFrame, side: pd.DataFrame, targets: pd.DataFrame,
                   play_table: dict[str, float]) -> pd.DataFrame:  # fmt: skip
    """Features for each target (game_id, team_id, player_id, point, t), all as of t."""
    sched = team_schedule(g)
    tg = targets.reset_index(drop=True).copy()
    sc = sched.set_index(["team_id", "game_id"])
    key = pd.MultiIndex.from_frame(tg[["team_id", "game_id"]])
    tg["idx"] = sc.idx.reindex(key).to_numpy()
    tg["rest"] = sc.rest.reindex(key).to_numpy()
    tg["b2b"] = sc.b2b.reindex(key).to_numpy()
    tg["season"] = tg.game_id.map(g.season)
    home = tg.game_id.map(g.home)
    tg["opp_id"] = np.where(tg.team_id == home, tg.game_id.map(g.away), home)
    own = asof_states(states, tg[["player_id", "t"]])
    cols = [c for c in states.columns if c not in ("player_id", "player_name", "game_id",
                                                   "tip", "known_at", "season", "team_id")]
    for c in cols:
        tg[c] = own[c].to_numpy()
    tg["has_state"] = own.known_at.notna().to_numpy()
    same_team = own.team_id.to_numpy() == tg.team_id.to_numpy()
    tg["streak"] = np.where(same_team, np.clip(tg.idx - tg.team_idx - 1, 0, 82), 0)
    tg["new_team"] = (~same_team).astype(float)
    tg["since_return"] = np.where(tg.streak >= 3, 0, tg.since_return)
    ist = injuries_asof(inj, tg[["game_id", "team_id", "player_id", "t"]])
    tg["status"] = ist.status.to_numpy()
    tg["p_play"] = play_probs(tg.status, tg.streak, ist.reported, play_table)
    tg["own_q"] = tg.status.isin(["Questionable", "Doubtful"]).astype(float)
    tg["own_prob"] = (tg.status == "Probable").astype(float)
    tg = tg.join(_rotation_context(p, sched, states, inj, tg, play_table))
    allow_keys = pd.DataFrame({"def_team": tg.opp_id, "role": tg.role.fillna("F"), "t": tg.t})
    tg = tg.join(_opp_factors(allow, allow_keys))
    s = side.set_index(["game_id", "point"])
    sk = pd.MultiIndex.from_frame(tg[["game_id", "point"]])
    margin = s.pred_margin.reindex(sk).to_numpy()
    total = s.pred_total.reindex(sk).to_numpy()
    sign = np.where(tg.team_id == home, 1.0, -1.0)
    tg["side_margin"] = sign * margin
    tg["side_total"] = total
    tg["abs_margin"] = np.abs(margin)
    return tg


def _rotation_context(p, sched, states, inj, tg, play_table) -> pd.DataFrame:
    """Per target: teammates' expected missing minutes and missing share of each stat (each
    teammate weighted by 1 - his play probability), excluding the player himself."""
    played = p[p.played][["team_id", "game_id", "player_id"]]
    idx = sched.set_index(["team_id", "game_id"]).idx
    played = played.assign(i=idx.reindex(pd.MultiIndex.from_frame(
        played[["team_id", "game_id"]])).to_numpy())  # fmt: skip
    slots = tg[["game_id", "team_id", "point", "t", "idx"]].drop_duplicates(
        ["game_id", "team_id", "point"])  # fmt: skip
    back = pd.concat([slots.assign(i=slots.idx - k) for k in range(1, ROTATION_GAMES + 1)])
    cand = back.merge(played[["team_id", "player_id", "i"]], on=["team_id", "i"])
    cand = cand.drop_duplicates(["game_id", "team_id", "point", "player_id"]).drop(columns="i")
    cand = cand.reset_index(drop=True)
    st = asof_states(states, cand[["player_id", "t"]])
    cand = cand[(st.team_id.to_numpy() == cand.team_id.to_numpy())].copy()
    st = st.loc[cand.index]
    cand["exp_min"] = st.min_l10.to_numpy()
    cand["start"] = st.start_l10.to_numpy()
    streak = np.clip(cand.idx.to_numpy() - st.team_idx.to_numpy() - 1, 0, 82)
    ist = injuries_asof(inj, cand[["game_id", "team_id", "player_id", "t"]])
    cand["q"] = 1 - play_probs(ist.status, pd.Series(streak), ist.reported, play_table)
    stats = ("pts", "reb", "ast", "fg3m", "pra", "fga")
    for s in stats:
        cand[f"e_{s}"] = st[f"g10_{s}"].to_numpy()
    k = ["game_id", "team_id", "point"]
    grp = cand.groupby(k)
    tot = pd.DataFrame({f"tot_{s}": grp[f"e_{s}"].sum() for s in stats})
    w = cand.assign(m_min=cand.q * cand.exp_min, m_start=cand.q * cand.exp_min * cand.start,
                    **{f"m_{s}": cand.q * cand[f"e_{s}"] for s in stats})  # fmt: skip
    mis = w.groupby(k)[["m_min", "m_start", *(f"m_{s}" for s in stats)]].sum()
    team = tot.join(mis)
    own = w.set_index([*k, "player_id"])
    tk = pd.MultiIndex.from_frame(tg[[*k, "player_id"]])
    out = pd.DataFrame(index=tg.index)
    team_rows = team.reindex(pd.MultiIndex.from_frame(tg[k]))
    own_rows = own.reindex(tk)
    out["mis_min"] = (team_rows.m_min.to_numpy() - own_rows.m_min.fillna(0).to_numpy())
    out["mis_start_min"] = (team_rows.m_start.to_numpy()
                            - own_rows.m_start.fillna(0).to_numpy())  # fmt: skip
    for s in stats:
        num = team_rows[f"m_{s}"].to_numpy() - own_rows[f"m_{s}"].fillna(0).to_numpy()
        den = team_rows[f"tot_{s}"].to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            out[f"mis_{s}"] = np.where(den > 0, num / den, 0.0)
    return out.fillna(0.0)


class Panel:
    """Everything the prop features read, loaded once: schedule, box rows, player states,
    opponent allowances and injury reports."""

    def __init__(self, g, p, inj):
        self.g, self.p, self.inj = g, p, inj
        self.sched = team_schedule(g)
        self.states = player_states(p, self.sched)
        self.allow = opponent_allowances(p, self.states)

    @classmethod
    def load(cls, conn: Connection, min_season: int | None = None) -> Panel:
        from betdata.nba.data import load_injuries

        g, p = load_box(conn, min_season)
        return cls(g, p, load_injuries(conn, g))

    def features(self, targets: pd.DataFrame, side: pd.DataFrame,
                 play_table: dict[str, float]) -> pd.DataFrame:  # fmt: skip
        return build_features(self.g, self.p, self.states, self.inj, self.allow, side, targets,
                              play_table)  # fmt: skip

    def played_targets(self, seasons=None, points=POINTS) -> pd.DataFrame:
        """History targets: every player-game with minutes, at each point, with the actuals."""
        d = self.p[self.p.played]
        if seasons is not None:
            d = d[d.season.isin(list(seasons))]
        t = d[["game_id", "team_id", "player_id"]].merge(
            point_times(self.g, d.game_id.unique(), points), on="game_id")  # fmt: skip
        return t
