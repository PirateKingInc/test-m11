"""M17: the engine's inputs per game and prediction point: both teams' `Side`s, from the as-of
rate store (team, defence, QB and player rates) and M12's play probabilities. No line.

Players: M12's candidates in groups QB, RB, WR, TE at the point. Receivers are the RB/WR/TE;
rushers the RB plus any WR/QB with designed carries in their history. QBs are ordered by
M12's role (the expected starter first); the x delta of a QB is the pass share times
`qb_weight` times (his EPA per dropback - the expected starter's).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from betdata.nfl.engine import drives as D
from betdata.nfl.engine.rates import RateStore
from betdata.nfl.engine.sim import Side

SKILL = ("QB", "RB", "WR", "TE")


def team_ids(engine=None) -> pd.DataFrame:
    """nflverse game id -> (home, away) abbreviations and main-database team ids."""
    from sqlalchemy import select

    from betdata.db.schema import game_source_ids, games
    from betdata.nfl.cli import _engine

    engine = engine or _engine()
    with engine.connect() as conn:
        rows = conn.execute(select(game_source_ids.c.source_game_id, games.c.home_team_id,
                                   games.c.away_team_id)
                            .join(games, games.c.game_id == game_source_ids.c.game_id)
                            .where(game_source_ids.c.source == "nflverse")).all()  # fmt: skip
    df = pd.DataFrame(rows, columns=["game_id", "home_id", "away_id"])
    parts = df.game_id.str.split("_", expand=True)
    df["away"], df["home"] = parts[2], parts[3]
    df["season"] = parts[0].astype(int)
    return df.set_index("game_id")


@dataclass
class Slot:
    game_id: str  # nflverse id
    point: str
    t: pd.Timestamp
    season: int
    home: str
    away: str


def team_rows(store: RateStore, slots: list[Slot]) -> pd.DataFrame:
    rows = []
    for s in slots:
        rows.append(dict(key=(s.game_id, s.point, 0), team=s.home, opp=s.away, season=s.season,
                         t=s.t))  # fmt: skip
        rows.append(dict(key=(s.game_id, s.point, 1), team=s.away, opp=s.home, season=s.season,
                         t=s.t))  # fmt: skip
    q = pd.DataFrame(rows)
    r = store.team(q)
    return pd.concat([q, r], axis=1)


ROLE_RATES = {"target_share": ("targets", "team_attempts"),
              "carry_share": ("carries", "team_runs"),
              "rz_target_share": ("rz_targets", "team_rz_attempts"),
              "rz_carry_share": ("rz_carries", "team_rz_runs")}  # fmt: skip


def role_coefs(store: RateStore, avail: pd.DataFrame, ids: pd.DataFrame,
               seasons=D.TRAIN) -> dict:  # fmt: skip
    """Per (rate, M12 group): share per unit of M12 role on the training seasons, so a
    player's prior share is coef x role (his own games then move him off it)."""
    a = avail[(avail.point == "final_report") & avail.group.isin(SKILL)
              & avail.nflverse_game_id.str[:4].astype(int).isin(seasons)].copy()  # fmt: skip
    a["team"] = np.where(a.team_id == a.nflverse_game_id.map(ids.home_id),
                         a.nflverse_game_id.map(ids.home), a.nflverse_game_id.map(ids.away))
    tg = store.team_games.set_index(["game_id", "team"])[["attempts", "runs"]]
    pl = store.players
    rz = pl.groupby(["game_id", "team"])[["rz_targets", "rz_carries"]].sum()
    vol = tg.join(rz).rename(columns={"attempts": "team_attempts", "runs": "team_runs",
                                      "rz_targets": "team_rz_attempts",
                                      "rz_carries": "team_rz_runs"})  # fmt: skip
    m = a.join(vol, on=["nflverse_game_id", "team"])
    own = pl.set_index(["game_id", "player"])[["targets", "carries", "rz_targets",
                                                "rz_carries"]]  # fmt: skip
    own = own[~own.index.duplicated()]
    m = m.join(own, on=["nflverse_game_id", "player_id"])
    out = {}
    for rate, (num, den) in ROLE_RATES.items():
        g = m.assign(x=m[num].fillna(0), d=m[den] * m.role).groupby("group")[["x", "d"]].sum()
        out[rate] = (g.x / g.d.replace(0, np.nan)).fillna(0).to_dict()
    return out


def player_rows(store: RateStore, avail: pd.DataFrame, slots: list[Slot],
                ids: pd.DataFrame, coefs: dict | None = None) -> pd.DataFrame:  # fmt: skip
    from betdata.nfl.engine.stability import positions

    pos = positions()
    want = {(s.game_id, s.point): s for s in slots}
    a = avail[avail.group.isin(SKILL)]
    a = a[[k in want for k in zip(a.nflverse_game_id, a.point, strict=True)]].copy()
    a = a[a.player_id.str.startswith("00-")]
    a["side"] = np.where(a.team_id == a.nflverse_game_id.map(ids.home_id), 0, 1)
    a["t"] = [want[k].t for k in zip(a.nflverse_game_id, a.point, strict=True)]
    a["season"] = [want[k].season for k in zip(a.nflverse_game_id, a.point, strict=True)]
    a["position"] = a.player_id.map(pos).fillna(a.group)
    q = a.rename(columns={"player_id": "player"})[["player", "season", "t", "position"]].copy()
    if coefs:
        for rate in ROLE_RATES:
            q[f"prior_{rate}"] = (a.group.map(coefs[rate]).fillna(0)
                                  * a.role.clip(lower=0.02)).to_numpy()  # fmt: skip
    r = store.player(q)
    return pd.concat([a.reset_index(drop=True), r.reset_index(drop=True)], axis=1)


def sides(tr: pd.DataFrame, pr: pd.DataFrame, slot: Slot, qb_weight: float,
          min_games: float = 0.0) -> tuple[Side, Side]:  # fmt: skip
    out = []
    for s in (0, 1):
        r = tr[tr.key == (slot.game_id, slot.point, s)].iloc[0]
        x = float(D.matchup(r.to_frame().T).iloc[0])
        off = float(D._logit(r.o_neutral_pass)[()] - D._logit(r.l_neutral_pass)[()])
        side = Side(x=x, offset=off, pace=float(r.o_pace / r.l_pace),
                    sack=float(r.o_sack + (r.d_sack - r.l_sack)),
                    scramble=float(r.o_scramble),
                    ypa=float(r.o_ypa + r.d_ypa - r.l_ypa), ypc=float(r.o_ypc + r.d_ypc - r.l_ypc),
                    pass_td_share=float(np.clip(r.o_pass_td_share, 0.3, 0.85)))  # fmt: skip
        p = pr[(pr.nflverse_game_id == slot.game_id) & (pr.point == slot.point) & (pr.side == s)]
        qbs = p[p.group == "QB"].sort_values("role", ascending=False)
        if len(qbs):
            ref = float(qbs.qb_epa.iloc[0])
            pr_pass = float(r.o_neutral_pass)
            side.qbs = [(q.player_id, float(1 - q.q), pr_pass * qb_weight * (float(q.qb_epa) - ref))
                        for q in qbs.head(3).itertuples()]  # fmt: skip
            qb_sack = float(qbs.qb_sack.iloc[0])
            side.sack = float(np.clip(0.5 * side.sack + 0.5 * qb_sack, 0.02, 0.15))
            side.scramble = float(np.clip(qbs.qb_scramble.iloc[0], 0.0, 0.2))
        rec = p[p.group.isin(("RB", "WR", "TE"))]
        side.rec = [(q.player_id, float(q.q), float(q.target_share), float(q.catch),
                     float(q.ypr) if q.ypr == q.ypr else 10.0, float(q.rz_target_share))
                    for q in rec.itertuples() if q.target_share > 0]  # fmt: skip
        rush = p[(p.group == "RB") | ((p.group.isin(("QB", "WR"))) & (p.carry_share > 0.03))]
        side.rush = [(q.player_id, float(q.q), float(q.carry_share), float(q.ypc),
                      float(q.rz_carry_share)) for q in rush.itertuples()]  # fmt: skip
        out.append(side)
    return out[0], out[1]
