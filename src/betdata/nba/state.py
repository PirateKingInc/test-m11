"""Incremental NBA league state over finished games (the `box` frame, in known_at order): Elo,
opponent-adjusted ratings, pace, the home-court estimate, standings, and each player's recent
minutes and value. Built by feeding rows, so a feature at instant t sees exactly the games
known by t (nfl.features.state.advanced rebuilds it if asked for an earlier prefix).

Also the per-game indexes of the injury and odds frames, built the same way.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from betdata.nba.box import STATS
from betdata.nfl.features.state import Rolling

ELO = dict(k=20.0, hfa=100.0, revert=0.25)  # FiveThirtyEight's NBA settings
ELO_MEAN, ELO_PER_POINT = 1500.0, 28.0
RATING_K = 0.04  # per-game step of the opponent-adjusted ratings (points per 100 possessions)
PACE_ALPHA = 0.08
SEASON_CARRY = 0.6  # share of a team's rating kept into the next season
HCA_ALPHA = 0.003  # the home-court estimate moves slowly (~330 games)
MIN_ALPHA = 0.15  # recent minutes: ~6 team games
VALUE_GAMES = 12.0  # shrinkage of a player's value towards replacement, in games
REPL_GMSC36, REPL_PM48 = 7.0, -2.0  # replacement level per 36 / 48 minutes
_IDX = {c: i for i, c in enumerate(("team_id", "player_id", "minutes", "points", "plus_minus",
                                    "starter", *STATS))}  # fmt: skip


def game_score(line) -> float:
    """Hollinger's game score from one box line."""
    g = {k: line[i] for k, i in _IDX.items()}
    return (g["points"] + 0.4 * g["fgm"] - 0.7 * g["fga"] - 0.4 * (g["fta"] - g["ftm"])
            + 0.7 * g["oreb"] + 0.3 * g["dreb"] + g["stl"] + 0.7 * g["ast"] + 0.7 * g["blk"]
            - 0.4 * g["pf"] - g["tov"])  # fmt: skip


@dataclass
class Player:
    team: int
    min_ew: float = 0.0  # recent minutes per team game (a missed game counts as zero)
    min_play: float = 0.0  # minutes per game when he plays (absences ignored)
    gmsc36: float = REPL_GMSC36
    pm48: float = REPL_PM48
    games: int = 0  # games played (lifetime, for shrinkage)
    streak: int = 0  # consecutive team games missed
    season: int | None = None
    last_season_team: int | None = None
    star: float = 0.0

    def value(self) -> tuple[float, float]:
        w = self.games / (self.games + VALUE_GAMES)
        return (w * self.gmsc36 + (1 - w) * REPL_GMSC36 - REPL_GMSC36,
                w * self.pm48 + (1 - w) * REPL_PM48 - REPL_PM48)


@dataclass
class League(Rolling):
    frame: str = "box"
    season: int | None = None
    elo: dict = field(default_factory=dict)
    off: dict = field(default_factory=dict)
    dfn: dict = field(default_factory=dict)
    off_fast: dict = field(default_factory=dict)
    dfn_fast: dict = field(default_factory=dict)
    pace: dict = field(default_factory=dict)
    league_ppp: float = 108.0  # points per 100 possessions
    league_pace: float = 98.0
    league_total: float = 210.0
    hca: float = 3.0  # home-court points
    players: dict = field(default_factory=dict)
    roster: dict = field(default_factory=dict)  # team -> set of player ids seen this season
    wins: dict = field(default_factory=dict)
    losses: dict = field(default_factory=dict)
    gp: dict = field(default_factory=dict)  # team -> games played this season
    prev_net: dict = field(default_factory=dict)  # team -> last season's final net rating
    prev_roster_min: dict = field(default_factory=dict)  # team -> {player: last season minutes}
    season_min: dict = field(default_factory=dict)  # (team, player) -> minutes this season

    def __post_init__(self):
        Rolling.__init__(self)

    # --- reads -----------------------------------------------------------------------
    def elo_of(self, team: int, season: int) -> float:
        r = self.elo.get(team, ELO_MEAN)
        if self.season is not None and season > self.season:
            r = ELO_MEAN + (r - ELO_MEAN) * (1 - ELO["revert"])
        return r

    def net(self, team: int, season: int, fast: bool = False) -> tuple[float, float]:
        """(offence, defence) in points per 100 above league (defence: + = concedes more)."""
        o = (self.off_fast if fast else self.off).get(team, 0.0)
        d = (self.dfn_fast if fast else self.dfn).get(team, 0.0)
        if self.season is not None and season > self.season:
            o, d = o * SEASON_CARRY, d * SEASON_CARRY
        return o, d

    # --- updates ---------------------------------------------------------------------
    def _new_season(self, season: int) -> None:
        if self.season is not None:
            teams = set(self.off) | set(self.dfn)
            self.prev_net = {t: self.off.get(t, 0.0) - self.dfn.get(t, 0.0) for t in teams}
            self.prev_roster_min = {}
            for (t, p), m in self.season_min.items():
                self.prev_roster_min.setdefault(t, {})[p] = m
            for d in (self.off, self.dfn, self.off_fast, self.dfn_fast):
                for t in d:
                    d[t] *= SEASON_CARRY
            self.elo = {t: ELO_MEAN + (r - ELO_MEAN) * (1 - ELO["revert"])
                        for t, r in self.elo.items()}  # fmt: skip
        self.season = season
        self.wins, self.losses, self.gp, self.season_min, self.roster = {}, {}, {}, {}, {}

    def step(self, row) -> None:
        season = row.season
        if self.season is None or season > self.season:
            self._new_season(season)
        h, a, hs, as_ = row.home, row.away, row.home_score, row.away_score
        neutral = bool(row.neutral)
        margin = hs - as_
        # Elo (FiveThirtyEight's margin-of-victory multiplier).
        d = self.elo_of(h, season) - self.elo_of(a, season) + (0 if neutral else ELO["hfa"])
        exp = 1 / (1 + 10 ** (-d / 400))
        win_d = d if margin > 0 else -d
        mult = ((abs(margin) + 3) ** 0.8) / (7.5 + 0.006 * win_d) if margin else 1.0
        shift = ELO["k"] * mult * ((margin > 0) - exp)
        self.elo[h] = self.elo_of(h, season) + shift
        self.elo[a] = self.elo_of(a, season) - shift
        # Possessions from the box lines.
        lines = row.players
        tot = {h: dict.fromkeys(("fga", "fta", "oreb", "tov"), 0.0),
               a: dict.fromkeys(("fga", "fta", "oreb", "tov"), 0.0)}  # fmt: skip
        for ln in lines:
            t = ln[0]
            if t in tot:
                for k in ("fga", "fta", "oreb", "tov"):
                    tot[t][k] += ln[_IDX[k]]
        poss = {t: v["fga"] + 0.44 * v["fta"] - v["oreb"] + v["tov"] for t, v in tot.items()}
        n_poss = (poss[h] + poss[a]) / 2 if lines and min(poss.values()) > 50 else None
        # Ratings (points per 100 possessions, opponent-adjusted, online).
        hca_side = 0.0 if neutral else self.hca / 2
        if n_poss:
            for (team, opp, pts, sign) in ((h, a, hs, 1), (a, h, as_, -1)):
                ppp = 100 * pts / n_poss
                for o, df_, k in ((self.off, self.dfn, RATING_K), (self.off_fast, self.dfn_fast,
                                                                   3 * RATING_K)):  # fmt: skip
                    expected = (self.league_ppp + o.get(team, 0.0) + df_.get(opp, 0.0)
                                + sign * 100 * hca_side / n_poss)
                    err = ppp - expected
                    o[team] = o.get(team, 0.0) + k * err
                    df_[opp] = df_.get(opp, 0.0) + k * err
            self.league_ppp += 0.004 * (100 * (hs + as_) / (2 * n_poss) - self.league_ppp)
            for team in (h, a):
                self.pace[team] = self.pace.get(team, 0.0) + PACE_ALPHA * (
                    n_poss - self.league_pace - self.pace.get(team, 0.0))
            self.league_pace += 0.004 * (n_poss - self.league_pace)
        self.league_total += 0.01 * (hs + as_ - self.league_total)
        if not neutral:
            oh, dh = self.net(h, season)
            oa, da = self.net(a, season)
            expected = ((oh - dh) - (oa - da)) * (n_poss or self.league_pace) / 100
            self.hca += HCA_ALPHA * (margin - expected - self.hca)
        # Standings.
        for team, won in ((h, margin > 0), (a, margin < 0)):
            self.gp[team] = self.gp.get(team, 0) + 1
            (self.wins if won else self.losses)[team] = (self.wins if won else
                                                         self.losses).get(team, 0) + 1
        # Players.
        played: dict[int, dict[str, tuple]] = {h: {}, a: {}}
        for ln in lines:
            if ln[0] in played and ln[_IDX["minutes"]] > 0:
                played[ln[0]][ln[1]] = ln
        if not lines:
            return
        for team in (h, a):
            seen = self.roster.setdefault(team, set())
            for pid, ln in played[team].items():
                mins = ln[_IDX["minutes"]]
                p = self.players.get(pid)
                if p is None:
                    p = self.players[pid] = Player(team)
                if p.team != team:
                    p.team = team
                p.min_ew += MIN_ALPHA * (mins - p.min_ew)
                p.min_play += (MIN_ALPHA if p.games else 1.0) * (mins - p.min_play)
                w = min(1.0, mins / 30) * 0.12
                p.gmsc36 += w * (36 * game_score(ln) / mins - p.gmsc36)
                p.pm48 += w * (48 * ln[_IDX["plus_minus"]] / mins - p.pm48)
                p.games += 1
                p.streak = 0
                seen.add(pid)
                key = (team, pid)
                self.season_min[key] = self.season_min.get(key, 0.0) + mins
            for pid in list(seen):
                if pid in played[team]:
                    continue
                p = self.players.get(pid)
                if p is None or p.team != team:
                    seen.discard(pid)
                    continue
                p.min_ew += MIN_ALPHA * (0.0 - p.min_ew)
                p.streak += 1

    # --- the team's players as of now ------------------------------------------------
    def rotation(self, team: int, min_minutes: float = 6.0,
                 max_missed: int = 20) -> list[tuple[str, Player]]:  # fmt: skip
        """Players of `team` this season with recent minutes or a recent absence: the ones
        whose availability matters. A player out for a long time keeps his pre-absence
        minutes as `base` (see features)."""
        out = []
        for pid in self.roster.get(team, ()):
            p = self.players.get(pid)
            if p is not None and p.team == team and p.min_play >= min_minutes \
                    and p.streak <= max_missed:
                out.append((pid, p))
        return out


@dataclass
class ByGame(Rolling):
    """Rows of a frame grouped by game_id, as known so far."""

    frame: str = ""
    rows: dict = field(default_factory=dict)

    def __post_init__(self):
        Rolling.__init__(self)

    def step(self, row) -> None:
        self.rows.setdefault(row.game_id, []).append(row)


def made_by_game(frame: str):
    def make():
        return ByGame(frame=frame)

    make.frame = frame
    return make


def elo_margin(state: League, g) -> float:
    d = state.elo_of(g.home, g.season) - state.elo_of(g.away, g.season)
    if not g.neutral:
        d += ELO["hfa"]
    return d / ELO_PER_POINT


def win_pct(state: League, team: int) -> float:
    n = state.gp.get(team, 0)
    return state.wins.get(team, 0) / n if n else 0.5


__all__ = ["League", "ByGame", "made_by_game", "elo_margin", "win_pct", "game_score", "math"]
