"""M15 NBA side variables (MODEL.md, "M15 NBA side variables"), as feature groups beside the
frozen M14 ones (nba.features.GROUPS is not changed). Every input is a finished game's box
score known by the prediction point, read through the as-of store.

  luck3  team and opponent 3P% over the last 15 games vs the league, shrunk with a 750-attempt
         prior: the points above expectation that should not repeat.
  load   load management: each team's top-2 players' chance of sitting by situation (second
         night of a back-to-back, weak opponent, late season with the standings settled),
         learned on training seasons, counted only while the report does not list him.
  integ  new-player integration: share of rotation minutes from players with fewer than 10
         games for the team; minutes joined / lost in the last 30 days.
  style  matchups over the last 15 games: offensive rebounding vs the opponent's defensive
         rebounding, 3PA rate vs threes allowed, turnovers forced vs ball security, FT rate vs
         opponent fouling.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from betdata.nba.box import STATS
from betdata.nba.features import _state, schedule_table, team_status
from betdata.nba.state import win_pct
from betdata.nfl.features.state import Rolling, advanced
from betdata.nfl.store import AsOf

WINDOW = 15
PRIOR_3PA = 750.0
NEW_GAMES = 10
RECENT_DAYS = 30
STAR_N = 2
_IDX = {c: i for i, c in enumerate(("team_id", "player_id", "minutes", "points", "plus_minus",
                                    "starter", *STATS))}  # fmt: skip
TEAM_STATS = ("fga", "fg3m", "fg3a", "fta", "oreb", "dreb", "tov", "pf")


@dataclass
class TeamBox(Rolling):
    """Each team's last WINDOW games of team totals (own and opponent), the league's running
    rates, and per (team, player) stint data."""

    frame: str = "box"
    games: dict = field(default_factory=dict)  # team -> deque of (own, opp) totals dicts
    league: dict = field(default_factory=dict)  # running league rates (EWM)
    joined: dict = field(default_factory=dict)  # (team, player) -> (first tip, games)
    last_team: dict = field(default_factory=dict)  # player -> (team, tip, minutes recent)

    def __post_init__(self):
        Rolling.__init__(self)

    def step(self, row) -> None:
        tot = {row.home: dict.fromkeys(TEAM_STATS, 0.0), row.away: dict.fromkeys(TEAM_STATS, 0.0)}
        for ln in row.players:
            t = ln[0]
            if t in tot:
                for k in TEAM_STATS:
                    tot[t][k] += ln[_IDX[k]]
        if not row.players or tot[row.home]["fga"] < 40 or tot[row.away]["fga"] < 40:
            return
        tip = row.known_at
        for team, opp in ((row.home, row.away), (row.away, row.home)):
            d = self.games.setdefault(team, deque(maxlen=WINDOW))
            d.append((tot[team], tot[opp]))
        both = {k: tot[row.home][k] + tot[row.away][k] for k in TEAM_STATS}
        rates = {"p3": both["fg3m"] / max(both["fg3a"], 1), "r3": both["fg3a"] / both["fga"],
                 "orb": both["oreb"] / max(both["oreb"] + both["dreb"], 1),
                 "tov": both["tov"] / (both["fga"] + 0.44 * both["fta"] + both["tov"]),
                 "ftr": both["fta"] / both["fga"], "fg3a": both["fg3a"] / 2}  # fmt: skip
        for k, v in rates.items():
            self.league[k] = v if k not in self.league else self.league[k] + 0.01 * (
                v - self.league[k])
        for ln in row.players:
            if ln[_IDX["minutes"]] <= 0 or ln[0] not in tot:
                continue
            key = (ln[0], ln[1])
            first, n = self.joined.get(key, (tip, 0))
            prev = self.last_team.get(ln[1])
            if prev is not None and prev[0] != ln[0]:
                first, n = tip, 0  # a new stint with this team
            self.joined[key] = (first, n + 1)
            self.last_team[ln[1]] = (ln[0], tip, ln[_IDX["minutes"]])

    def sums(self, team: int) -> tuple[dict, dict, int]:
        d = self.games.get(team, ())
        own = {k: sum(g[0][k] for g in d) for k in TEAM_STATS}
        opp = {k: sum(g[1][k] for g in d) for k in TEAM_STATS}
        return own, opp, len(d)


def _box(a: AsOf) -> TeamBox:
    return advanced(a, "teambox15", TeamBox)


def _poss(t: dict) -> float:
    return t["fga"] + 0.44 * t["fta"] - t["oreb"] + t["tov"]


@dataclass(frozen=True)
class Luck3:
    name: str = "luck3"

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        tb = _box(a)
        lg = tb.league.get("p3", 0.36)
        out = {}
        for side, team in (("h", g.home), ("a", g.away)):
            own, opp, n = tb.sums(team)
            if not n:
                out[side] = (0.0, 0.0)
                continue
            # Points above expectation per game from shooting luck, shrunk toward the league.
            def luck(made, att, n=n):
                shrunk = (made + PRIOR_3PA * lg) / (att + PRIOR_3PA)
                return 3 * (shrunk - lg) * att / n
            out[side] = (luck(own["fg3m"], own["fg3a"]), luck(opp["fg3m"], opp["fg3a"]))
        (oh, dh), (oa, da) = out["h"], out["a"]
        return {"m_luck3_net": (oh - dh) - (oa - da), "t_luck3_sum": oh + dh + oa + da}


def situation(a: AsOf, g, team: int) -> str:
    """b2b|weak|late bits for a team in game g."""
    s = schedule_table(a.store)[(g.game_id, team)]
    st = _state(a)
    opp = g.away if team == g.home else g.home
    weak = win_pct(st, opp) < 0.4 and st.gp.get(opp, 0) >= 10
    gp, wp = st.gp.get(team, 0), win_pct(st, team)
    late = g.season_type == "REG" and gp >= 65 and (wp > 0.68 or wp < 0.32)
    return f"{int(s['b2b'])}{int(weak)}{int(late)}"


def stars(a: AsOf, team: int) -> list[tuple[str, float]]:
    """The team's top-2 rotation players by minutes x value, with their value share."""
    st = _state(a)
    rot = st.rotation(team)
    scored = sorted(((pid, p.min_play * max(p.value()[0], 0.0) / 36) for pid, p in rot),
                    key=lambda x: -x[1])  # fmt: skip
    return scored[:STAR_N]


@dataclass(frozen=True)
class Load:
    name: str = "load"

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        table = a.store.params.get("load_sit") or {}
        out = {}
        for side, team in (("h", g.home), ("a", g.away)):
            status, _ = team_status(a, g, team)
            p = table.get(situation(a, g, team), table.get("000", 0.03))
            base = table.get("000", 0.03)
            out[side] = sum((p - base) * v for pid, v in stars(a, team) if pid not in status)
        return {"m_load_exp": -(out["h"] - out["a"])}


@dataclass(frozen=True)
class Integration:
    name: str = "integ"

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        tb, st = _box(a), _state(a)
        t = a.t
        out = {}
        for side, team in (("h", g.home), ("a", g.away)):
            rot = st.rotation(team)
            tot = sum(p.min_play for _, p in rot) or 1.0
            new = sum(p.min_play for pid, p in rot
                      if tb.joined.get((team, pid), (t, 0))[1] < NEW_GAMES)  # fmt: skip
            recent = sum(p.min_play for pid, p in rot
                         if (t - tb.joined.get((team, pid), (t, 0))[0]).days < RECENT_DAYS
                         and tb.joined.get((team, pid), (t, 0))[1] >= 1)  # fmt: skip
            lost = sum(m for pid, (tm, tip, m) in tb.last_team.items()
                       if tm != team and (team, pid) in tb.joined
                       and (t - tip).days < RECENT_DAYS
                       and tb.joined[(team, pid)][0] < tip)  # fmt: skip
            out[side] = (new / tot, recent / tot, lost / 240)
        (nh, rh, lh), (na, ra, la) = out["h"], out["a"]
        return {"m_integ_new": nh - na, "m_integ_joined": rh - ra, "m_integ_lost": lh - la}


@dataclass(frozen=True)
class Style:
    name: str = "style"

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        tb = _box(a)
        lg = tb.league
        if not lg:
            return {"m_style_reb": 0.0, "m_style_3pa": 0.0, "m_style_tov": 0.0,
                    "m_style_ft": 0.0, "t_style_3pa": 0.0}  # fmt: skip
        r = {}
        for side, team in (("h", g.home), ("a", g.away)):
            own, opp, n = tb.sums(team)
            if n < 5:
                r[side] = dict.fromkeys(("orb", "drb", "r3", "r3a", "tov", "tovf", "ftr",
                                         "ftra"), 0.0)  # fmt: skip
                continue
            w = n / (n + 10)  # shrink thin windows
            r[side] = {
                "orb": w * (own["oreb"] / max(own["oreb"] + opp["dreb"], 1) - lg["orb"]),
                "drb": w * (opp["oreb"] / max(opp["oreb"] + own["dreb"], 1) - lg["orb"]),
                "r3": w * (own["fg3a"] / own["fga"] - lg["r3"]),
                "r3a": w * (opp["fg3a"] / opp["fga"] - lg["r3"]),
                "tov": w * (own["tov"] / (_poss(own) + own["oreb"]) - lg["tov"]),
                "tovf": w * (opp["tov"] / (_poss(opp) + opp["oreb"]) - lg["tov"]),
                "ftr": w * (own["fta"] / own["fga"] - lg["ftr"]),
                "ftra": w * (opp["fta"] / opp["fga"] - lg["ftr"])}  # fmt: skip
        h, w_ = r["h"], r["a"]
        # Each side's edge where its strength meets the opponent's weakness, home minus away.
        return {"m_style_reb": h["orb"] * w_["drb"] * 100 - w_["orb"] * h["drb"] * 100
                + (h["orb"] - w_["orb"]) * 10,
                "m_style_3pa": h["r3"] * w_["r3a"] * 100 - w_["r3"] * h["r3a"] * 100,
                "m_style_tov": h["tovf"] * w_["tov"] * 100 - w_["tovf"] * h["tov"] * 100,
                "m_style_ft": h["ftr"] * w_["ftra"] * 100 - w_["ftr"] * h["ftra"] * 100,
                "t_style_3pa": (h["r3"] + w_["r3a"] + w_["r3"] + h["r3a"])}  # fmt: skip


def fit_load(store, seasons) -> dict[str, float]:
    """P(a top-2 player sits | situation) on training seasons: among stars who played their
    team's previous game and were not on the report at the pre-tip point, the share who did
    not play, per situation key (b2b|weak|late)."""
    from betdata.nba.pipeline import played, tasks

    who = played(store)
    counts: dict[str, list[int]] = {}
    gids = [gid for gid, s in store.games.season.items() if s in seasons and gid in who]
    for t, gid, _p in tasks(store, gids, ("pretip",)):
        g = store.games.loc[gid]
        a = AsOf(store, t)
        st = _state(a)
        for team in (g.home, g.away):
            status, _ = team_status(a, g, team)
            key = situation(a, g, team)
            for pid, _v in stars(a, team):
                pl = st.players.get(pid)
                if pid in status or pl is None or pl.streak > 0:
                    continue
                c = counts.setdefault(key, [0, 0])
                c[0] += pid not in who[gid]
                c[1] += 1
    return {k: (y + 1) / (n + 20) for k, (y, n) in counts.items() if n >= 30} | {
        f"n:{k}": float(n) for k, (_, n) in counts.items()}


GROUPS15 = {grp.name: grp for grp in (Luck3(), Load(), Integration(), Style())}
ORDER15 = ["luck3", "load", "integ", "style"]
