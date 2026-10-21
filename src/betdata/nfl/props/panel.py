"""The NFL prop model's player-game panel, as of each M12 prediction point (MODEL.md, M15).

Everything comes from time-stamped rows (nfl/props/data.py; kickoff + 12 h for a game's data):
  states   a player's role and production after each of his played games: snap share, route
           participation (on the field for a dropback; snap share where participation is
           missing, e.g. 2026), target, air-yards, carry and pass-attempt shares, red-zone
           targets and carries, over his last 6 games, the season so far and exponentially
           weighted; per-target/carry/attempt rates and man/zone splits over his last 16 games.
           Read for a later game only once known (`known_at <= t`).
  team     the team's pass attempts, targets, carries and dropbacks (last 6, season), and as a
           defence: volume, yards and receptions allowed by position, man/zone rate.
  avail    M12's candidates and play probabilities by status (features/availability.py) at
           the point: teammates' targets and carries are reassigned by share, each teammate
           weighted by 1 - his play probability.
  side     the side model's margin and total and the M13 weather at the point (nfl/props/side.py).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from betdata.nfl.points import FINAL_REPORT, POINTS, point_time

ROLL = ("snap", "route_f", "tgt_share", "car_share", "att_share", "air_share", "tgt", "rec",
        "car", "att", "rec_yds", "rush_yds", "pass_yds", "rz_tgt", "rz_car", "td")  # fmt: skip
EWM = ("tgt_share", "car_share", "att_share", "route_f")
G10 = ("rec_yds", "rush_yds", "pass_yds", "rec", "td")
SUMS = ("tgt", "rec", "rec_yds", "car", "rush_yds", "att", "pass_yds", "routes_f", "routes_man",
        "routes_zone", "yds_man", "yds_zone", "td")  # fmt: skip
RATE_GAMES = 16
TEAM_GAMES = 6
OPP_GAMES = 10
LEAGUE_ROWS = 320  # league allowances: the last ~320 team-games (about 10 weeks)
OPP_PRIOR = {"tgt": 120.0, "car": 150.0, "att": 200.0, "cov": 200.0}
REC_POS = ("WR", "TE", "RB")
TEAM_COLS = ("t_att", "t_tgt", "t_car", "dropbacks")


def _ratio(a, b) -> np.ndarray:
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(b > 0, a / b, np.nan)


def player_states(box: pd.DataFrame, team: pd.DataFrame) -> pd.DataFrame:
    """A player's state after each of his played games (known at that game's known_at)."""
    t = team.set_index(["game_id", "team_id"])
    k = pd.MultiIndex.from_frame(box[["game_id", "team_id"]])
    d = box.copy()
    for c in ("t_att", "t_tgt", "t_car", "t_air", "dropbacks"):
        d[c] = t[c].reindex(k).to_numpy()
    d["tgt_share"] = _ratio(d.tgt, d.t_tgt)
    d["car_share"] = _ratio(d.car, d.t_car)
    d["att_share"] = _ratio(d.att, d.t_att)
    d["air_share"] = np.nan_to_num(_ratio(d.air, d.t_air))
    d["route"] = np.where(d.has_part, _ratio(d.routes, d.dropbacks), np.nan)
    d["route_f"] = d.route.fillna(d.snap).fillna(0.0)
    d["routes_f"] = d.route_f * d.dropbacks.fillna(0)
    d = d.sort_values(["player_id", "kickoff"]).reset_index(drop=True)
    by = d.groupby("player_id", sort=False)
    out = d[["player_id", "name", "pos", "team_id", "game_id", "season", "kickoff",
             "known_at"]].copy()  # fmt: skip
    cols = list(ROLL)
    out[[f"l6_{c}" for c in cols]] = by[cols].rolling(6, 1).mean().to_numpy()
    out[[f"g10_{c}" for c in G10]] = by[list(G10)].rolling(10, 1).mean().to_numpy()
    out[[f"s16_{c}" for c in SUMS]] = by[list(SUMS)].rolling(RATE_GAMES, 1).sum().to_numpy()
    out[[f"ew_{c}" for c in EWM]] = by[list(EWM)].transform(
        lambda s: s.ewm(halflife=3).mean()).to_numpy()  # fmt: skip
    bs = d.groupby(["player_id", "season"], sort=False)
    out[[f"sn_{c}" for c in cols]] = bs[cols].expanding().mean().to_numpy()
    out["n_season"] = bs.cumcount() + 1
    out["n_career"] = by.cumcount() + 1
    out["sn_td_rate"] = out.sn_td
    return out


def team_states(team: pd.DataFrame, box: pd.DataFrame) -> pd.DataFrame:
    """Per (team, game) after the game: offence volume (last 6, season) and, as a defence,
    the allowances over its last OPP_GAMES and the league's over its last LEAGUE_ROWS."""
    t = team.sort_values(["team_id", "kickoff"]).reset_index(drop=True)
    by = t.groupby("team_id", sort=False)
    out = t[["team_id", "game_id", "known_at"]].copy()
    cols = list(TEAM_COLS)
    out[[f"tl6_{c}" for c in cols]] = by[cols].rolling(TEAM_GAMES, 1).mean().to_numpy()
    bs = t.groupby(["team_id", "season"], sort=False)
    out[[f"tsn_{c}" for c in cols]] = bs[cols].expanding().mean().to_numpy()
    return out


def defense_rows(box: pd.DataFrame, team: pd.DataFrame) -> pd.DataFrame:
    """Per (game, defence): what it allowed, and its man and zone snaps."""
    b = box.assign(def_team=box.opp_id)
    parts = {"car": b.car, "rush_yds": b.rush_yds, "att": b.att, "pass_yds": b.pass_yds}
    for p in REC_POS:
        m = (b.pos == p).astype(float)
        parts.update({f"tgt_{p}": b.tgt * m, f"rec_{p}": b.rec * m, f"yds_{p}": b.rec_yds * m})
    a = pd.DataFrame(parts).assign(game_id=b.game_id, def_team=b.def_team)
    a = a.groupby(["game_id", "def_team"]).sum()
    tm = team.set_index(["game_id", "team_id"])
    a[["def_man", "def_zone"]] = tm[["def_man", "def_zone"]].reindex(a.index).fillna(0).to_numpy()
    a = a.reset_index()
    a["known_at"] = a.game_id.map(team.drop_duplicates("game_id").set_index("game_id").known_at)
    return a


def defense_states(d: pd.DataFrame) -> pd.DataFrame:
    d = d.sort_values("known_at").reset_index(drop=True)
    vals = [c for c in d.columns if c not in ("game_id", "def_team", "known_at")]
    out = d[["def_team", "game_id", "known_at"]].copy()
    out[[f"o_{c}" for c in vals]] = d.groupby("def_team", sort=False)[vals].rolling(
        OPP_GAMES, 1).sum().reset_index(level=0, drop=True).sort_index().to_numpy()  # fmt: skip
    out[[f"lg_{c}" for c in vals]] = d[vals].rolling(LEAGUE_ROWS, 1).sum().to_numpy()
    return out


def _asof(states: pd.DataFrame, keys: pd.DataFrame, by: str, at: str = "t") -> pd.DataFrame:
    """For each row of keys: the latest state of `keys[by]` known by `keys[at]`."""
    k = keys.reset_index(drop=True).reset_index().rename(columns={"index": "_row"})
    k = k.sort_values(at)
    s = states.sort_values("known_at")
    if not pd.api.types.is_numeric_dtype(s[by]):  # gsis ids: one string dtype on both sides
        k[by], s = k[by].astype(object), s.assign(**{by: s[by].astype(object)})
    m = pd.merge_asof(k, s, left_on=at, right_on="known_at", by=by, allow_exact_matches=True,
                      suffixes=("", "_s"))  # fmt: skip
    return m.sort_values("_row").reset_index(drop=True)


def _shrunk(num, den, prior_n, league) -> np.ndarray:
    num, den = np.nan_to_num(np.asarray(num, float)), np.nan_to_num(np.asarray(den, float))
    return (num + prior_n * league) / (den + prior_n)


def opponent_factors(dstate: pd.DataFrame, keys: pd.DataFrame) -> pd.DataFrame:
    """keys: def_team, t, pos. The defence's allowed rates over its last games known by t,
    relative to the league's, shrunk strongly toward 1; plus its man rate vs the league's."""
    m = _asof(dstate.rename(columns={"def_team": "def_team"}), keys, "def_team")
    out = pd.DataFrame(index=m.index)
    pos = m.pos.where(m.pos.isin(REC_POS), "WR").to_numpy()

    def pick(prefix, what):
        cols = {p: m[f"{prefix}{what}_{p}"].to_numpy(dtype=float) for p in REC_POS}
        return np.select([pos == p for p in REC_POS], [cols[p] for p in REC_POS])

    for name, num, den, prior in (
        ("ypt", "yds", "tgt", OPP_PRIOR["tgt"]), ("rpt", "rec", "tgt", OPP_PRIOR["tgt"])):
        lg = _ratio(pick("lg_", num), pick("lg_", den))
        out[f"opp_{name}"] = _ratio(_shrunk(pick("o_", num), pick("o_", den), prior, lg), lg)
    for name, num, den, prior in (("ypc", "rush_yds", "car", OPP_PRIOR["car"]),
                                  ("ypa", "pass_yds", "att", OPP_PRIOR["att"])):
        lg = _ratio(m[f"lg_{num}"], m[f"lg_{den}"])
        out[f"opp_{name}"] = _ratio(_shrunk(m[f"o_{num}"], m[f"o_{den}"], prior, lg), lg)
    lg_man = _ratio(m.lg_def_man, m.lg_def_man + m.lg_def_zone)
    opp_man = _shrunk(m.o_def_man, m.o_def_man + m.o_def_zone, OPP_PRIOR["cov"], lg_man)
    out["opp_man_diff"] = np.nan_to_num(opp_man - lg_man)
    return out.fillna({"opp_ypt": 1.0, "opp_rpt": 1.0, "opp_ypc": 1.0, "opp_ypa": 1.0})


# --- availability (M12's candidates and play probabilities) ------------------------------------


def availability(store, slots: pd.DataFrame) -> pd.DataFrame:
    """M12 candidates for both teams of each (game_id, point, t) slot: game_id, point, team_id,
    player_id, group, role, bucket, p_play. Slots are visited in time order (M12's rolling
    states only move forward). Point `early` (before the final report) reads as the final
    report, as M12's live predictions do."""
    from betdata.nfl.features.availability import candidates, play_prob
    from betdata.nfl.store import AsOf

    rows = []
    for s in slots.sort_values("t").itertuples(index=False):
        g = store.games.loc[s.game_id]
        a = AsOf(store, s.t)
        pt = s.point if s.point in POINTS else FINAL_REPORT
        for team in (g.home, g.away):
            for k, grp, role, bucket in candidates(a, g, team, pt):
                rows.append((s.game_id, s.point, team, k, grp, role, bucket,
                             play_prob(store.params, bucket)))  # fmt: skip
    return pd.DataFrame(rows, columns=["game_id", "point", "team_id", "player_id", "group",
                                       "role", "bucket", "p_play"])  # fmt: skip


def redistribute(share: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Each player's missing share of the team's volume from teammates who may not play:
    sum over teammates j != i of q_j * share_j, over the sum of all shares. A player's
    expected share if he plays is share / (1 - missing): with one teammate surely out, the
    others' adjusted shares add up to the team's total again."""
    share = np.nan_to_num(np.asarray(share, dtype=float))
    q = np.asarray(q, dtype=float)
    tot = share.sum()
    if tot <= 0:
        return np.zeros_like(share)
    return ((q * share).sum() - q * share) / tot


def healthy_play(params: dict) -> dict[str, float]:
    """M12's play probability of a player with no status, per point: what `q` is relative to."""
    from betdata.nfl.features.availability import play_prob

    fr = play_prob(params, "fr|-|-")
    return {"final_report": fr, "early": fr, "prekick": play_prob(params, "pk_act|-")}


def teammate_context(avail: pd.DataFrame, states: pd.DataFrame, slots: pd.DataFrame,
                     healthy: dict[str, float]) -> pd.DataFrame:  # fmt: skip
    """Per (game_id, point, team_id, player_id) candidate: missing target, carry and attempt
    shares (`mis_*`), his own play probability and status bucket, and teammates likely out.
    A teammate's chance of missing is q = 1 - p_play / p_healthy (M12's play probability
    relative to that of a player with no status at the point), so a teammate who is not on
    the report counts as fully available."""
    a = avail[avail.group.isin(["QB", "RB", "WR", "TE"])].merge(
        slots[["game_id", "point", "t"]], on=["game_id", "point"])  # fmt: skip
    st = _asof(states[["player_id", "known_at", "team_id", "l6_tgt_share", "l6_car_share",
                       "l6_att_share", "name"]], a[["player_id", "t"]], "player_id")  # fmt: skip
    a = a.reset_index(drop=True)
    same = st.team_id.to_numpy() == a.team_id.to_numpy()
    a["name"] = st.name.to_numpy()
    base = a.point.map(healthy).fillna(0.9).to_numpy()
    q = np.clip(1 - a.p_play.to_numpy() / base, 0.0, 1.0)
    for s in ("tgt", "car", "att"):
        a[f"e_{s}"] = np.where(same, st[f"l6_{s}_share"].fillna(0).to_numpy(), 0.0)
    a["q"] = q
    out = []
    for _, grp in a.groupby(["game_id", "point", "team_id"], sort=False):
        r = grp[["game_id", "point", "team_id", "player_id", "p_play", "bucket"]].copy()
        for s in ("tgt", "car", "att"):
            r[f"mis_{s}"] = redistribute(grp[f"e_{s}"].to_numpy(), grp.q.to_numpy())
        load = grp.e_tgt + grp.e_car
        outs = grp[(grp.q >= 0.5) & (load >= 0.08)].sort_values("e_tgt", ascending=False)
        r["out_names"] = [[n for p, n in zip(outs.player_id, outs.name, strict=True)
                           if p != pid and isinstance(n, str)][:2]
                          for pid in r.player_id]  # fmt: skip
        out.append(r)
    cols = ["game_id", "point", "team_id", "player_id", "p_play", "bucket", "mis_tgt", "mis_car",
            "mis_att", "out_names"]  # fmt: skip
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=cols)


def point_times(g: pd.DataFrame, game_ids, points=POINTS) -> pd.DataFrame:
    rows = [(gid, p, point_time(p, g.at[gid, "kickoff"])) for gid in game_ids for p in points]
    out = pd.DataFrame(rows, columns=["game_id", "point", "t"])
    out["t"] = pd.to_datetime(out.t, utc=True)
    return out


class Panel:
    """Everything the prop features read, loaded once."""

    def __init__(self, g: pd.DataFrame, box: pd.DataFrame, team: pd.DataFrame):
        self.g, self.box, self.team = g, box, team
        self.states = player_states(box, team)
        self.tstates = team_states(team, box)
        self.dstates = defense_states(defense_rows(box, team))

    @classmethod
    def load(cls, conn, store, seasons, data_dir, cached: bool = False) -> Panel:
        from betdata.nfl.props.data import load_box, load_box_cached

        load = load_box_cached if cached else load_box
        box, team = load(conn, store.games, store.frame("snaps").df, seasons, data_dir)
        if box.empty:
            raise ValueError("no NFL player stats on disk or cached (nflverse_extra player_stats)")
        return cls(store.games, box, team)

    def played_targets(self, seasons, points=POINTS) -> pd.DataFrame:
        d = self.box[self.box.season.isin(list(seasons))]
        d = d[self.g.loc[d.game_id, "season_type"].isin(["REG", "POST"]).to_numpy()]
        return d[["game_id", "team_id", "player_id"]].merge(
            point_times(self.g, d.game_id.unique(), points), on="game_id")  # fmt: skip

    def features(self, targets: pd.DataFrame, side: pd.DataFrame, mates: pd.DataFrame,
                 ) -> pd.DataFrame:  # fmt: skip
        return build_features(self, targets, side, mates)


def build_features(panel: Panel, targets: pd.DataFrame, side: pd.DataFrame,
                   mates: pd.DataFrame) -> pd.DataFrame:  # fmt: skip
    """Features for each target (game_id, team_id, player_id, point, t), all as of t."""
    g = panel.g
    tg = targets.reset_index(drop=True).copy()
    own = _asof(panel.states, tg[["player_id", "t"]], "player_id")
    skip = {"player_id", "game_id", "kickoff", "known_at", "season", "_row", "t"}
    for c in own.columns:
        if c not in skip:
            tg[c if c != "team_id" else "state_team"] = own[c].to_numpy()
    tg["has_state"] = own.known_at.notna().to_numpy()
    tg["new_team"] = (tg.state_team != tg.team_id).astype(float)
    tg["season"] = tg.game_id.map(g.season)
    tg["new_season"] = (own.season.to_numpy() != tg.season.to_numpy()).astype(float)
    home = tg.game_id.map(g.home)
    tg["opp_id"] = np.where(tg.team_id == home, tg.game_id.map(g.away), home)
    ts = _asof(panel.tstates, tg[["team_id", "t"]], "team_id")
    for c in ts.columns:
        if c.startswith(("tl6_", "tsn_")):
            tg[c] = ts[c].to_numpy()
    of = opponent_factors(panel.dstates, pd.DataFrame(
        {"def_team": tg.opp_id.to_numpy(), "t": tg.t.to_numpy(), "pos": tg.pos.to_numpy()}))
    tg = pd.concat([tg, of.set_index(tg.index)], axis=1)
    os_ = _asof(panel.tstates.rename(columns={"team_id": "opp_id"}), tg[["opp_id", "t"]],
                "opp_id")  # fmt: skip
    for c in TEAM_COLS:  # the opponent's own offence volume (game pace)
        tg[f"opp_tl6_{c}"] = os_[f"tl6_{c}"].to_numpy()
    s = side.set_index(["game_id", "point"])
    sk = pd.MultiIndex.from_frame(tg[["game_id", "point"]])
    sign = np.where(tg.team_id == home, 1.0, -1.0)
    tg["side_margin"] = sign * s.pred_margin.reindex(sk).to_numpy()
    tg["side_total"] = s.pred_total.reindex(sk).to_numpy()
    tg["wind"] = s.wind.reindex(sk).fillna(0).to_numpy()
    tg["rain"] = s.rain.reindex(sk).fillna(0).to_numpy()
    m = mates.set_index(["game_id", "point", "team_id", "player_id"])
    mk = pd.MultiIndex.from_frame(tg[["game_id", "point", "team_id", "player_id"]])
    for c in ("mis_tgt", "mis_car", "mis_att"):
        tg[c] = m[c].reindex(mk).fillna(0.0).to_numpy()
    tg["p_play"] = m.p_play.reindex(mk).fillna(0.95).to_numpy()
    bucket = m.bucket.reindex(mk).fillna("").astype(str)
    tg["own_q"] = bucket.str.contains("Questionable|Doubtful").astype(float).to_numpy()
    tg["out_names"] = m.out_names.reindex(mk).to_numpy()
    return tg
