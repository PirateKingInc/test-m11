"""A synthetic NBA league for the M20 engine tests: frames shaped like the database's (games,
box lines, stints), built in memory. Four teams, ten players each, a game a day."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from betdata.nba.engine import data as E

T0 = datetime(2021, 10, 20, 23, 30, tzinfo=UTC)
TEAMS = (1, 2, 3, 4)


def roster(team: int) -> list[str]:
    return [f"{team}{i:02d}" for i in range(10)]


def league(n_days: int = 60, seed: int = 3, season: int = 2021, minutes=None, moves=None,
           blowouts=()):  # fmt: skip
    """(g, p, stints). `minutes(team, i, day)` overrides a player's minutes (0 = DNP);
    `moves` {player: (day, new team)} trades a player; `blowouts` days end 30-point games."""
    rng = np.random.default_rng(seed)
    moves = moves or {}
    G, P, S = [], [], []
    for day in range(n_days):
        tip = T0 + timedelta(days=day)
        h, a = (TEAMS[day % 4], TEAMS[(day + 1 + day // 4) % 4])
        if h == a:
            a = TEAMS[(TEAMS.index(h) + 1) % 4]
        gid = 1000 + day
        G.append(dict(game_id=gid, season=season, season_type="REG", tip=tip, home=h, away=a,
                      neutral=False, status="final", home_abbr=f"T{h}", away_abbr=f"T{a}"))
        lineups = {}
        for team in (h, a):
            players = [x for t in TEAMS for x in roster(t)
                       if _team_of(x, day, moves) == team]  # fmt: skip
            base = {pid: (34 - 2.5 * int(pid[-2:])) for pid in players}
            mins = {}
            for pid in players:
                m = base[pid] if minutes is None else minutes(team, pid, day)
                mins[pid] = max(0.0, m)
            tot = sum(mins.values()) or 1
            mins = {k: v * 240 / tot for k, v in mins.items()}
            lineups[team] = sorted(mins, key=lambda k: -mins[k])[:5]
            for pid, m in mins.items():
                fga = rng.poisson(0.38 * m)
                fg3a = rng.binomial(fga, 0.4)
                fgm = rng.binomial(fga, 0.46)
                fg3m = min(fgm, rng.binomial(fg3a, 0.36))
                fta = rng.poisson(0.1 * m)
                ftm = rng.binomial(fta, 0.78)
                P.append(dict(game_id=gid, team_id=team, player_id=pid, player_name=f"P {pid}",
                              starter=pid in lineups[team], did_not_play=m == 0,
                              known_at=tip + timedelta(hours=3), minutes=round(m, 1),
                              points=2 * (fgm - fg3m) + 3 * fg3m + ftm, fgm=fgm, fga=fga,
                              fg3m=fg3m, fg3a=fg3a, ftm=ftm, fta=fta,
                              oreb=rng.poisson(0.03 * m), dreb=rng.poisson(0.12 * m),
                              ast=rng.poisson(0.08 * m), stl=0, blk=0,
                              tov=rng.poisson(0.05 * m), pf=rng.poisson(0.07 * m)))  # fmt: skip
        # stints: four periods, one stint each, the top five; home leads by 30 on blowout days
        lead = 8 if day in blowouts else 1
        bounds = [(1, 0, 720), (2, 720, 1440), (3, 1440, 2160), (4, 2160, 2580), (4, 2580, 2880)]
        for per, a0, a1 in bounds:
            S.append(dict(game_id=gid, period=per, start_sec=float(a0), end_sec=float(a1),
                          home_players=",".join(sorted(lineups[h])),
                          away_players=",".join(sorted(lineups[a])),
                          home_pts=round((26 + lead) * (a1 - a0) / 720),
                          away_pts=round(26 * (a1 - a0) / 720),
                          home_poss=24.0, away_poss=24.0,
                          known_at=tip + timedelta(hours=3)))  # fmt: skip
    g = pd.DataFrame(G)
    g["tip"] = pd.to_datetime(g.tip, utc=True)
    g = g.set_index("game_id", drop=False)
    p = pd.DataFrame(P)
    p["known_at"] = pd.to_datetime(p.known_at, utc=True)
    st = pd.DataFrame(S)
    return g, p, st


def _team_of(pid: str, day: int, moves: dict) -> int:
    if pid in moves and day >= moves[pid][0]:
        return moves[pid][1]
    return int(pid[0])


def frames(**kw):
    g, p, st = league(**kw)
    g2, p2, tg = E.build(g, p.copy(), st)
    return g2, p2, tg, st


NO_INJ = pd.DataFrame({"game_id": pd.Series(dtype="int64"), "team_id": pd.Series(dtype="int64"),
                       "player_id": pd.Series(dtype=object), "player_name": pd.Series(dtype=object),
                       "status": pd.Series(dtype=object),
                       "known_at": pd.Series(dtype="datetime64[us, UTC]")})  # fmt: skip
