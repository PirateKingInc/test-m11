"""Group 1: player availability.

Each player has a *role*: his average share of his unit's snaps over his last few games played
(any team, so a new arrival brings his role with him). Quarterbacks also have a *value*: EPA
per dropback, recency-weighted over his career, shrunk toward replacement level and scaled to
a game (points of EPA above a replacement QB).

For each team and prediction point:
  * candidates: the latest known roster, this week's injury report, and recent snap counts;
  * each candidate gets a chance of playing, from his report/practice status (final report)
    or the game-day roster (prekick: active or inactive), learned on training seasons only
    (`learn_play_prob`, kept in store.params["play_prob"]);
  * a position group's expected strength = the best `slots` (P(play) x role) among its
    candidates; its recent strength = the same from the team's last 4 games' snap counts. The
    feature is the change (expected minus recent): the team ratings already price in the
    players who have been playing, so only the change is new information;
  * QB: expected value (starter and backup weighted by the starter's chance of playing),
    change against the QBs of recent games, and the backup's inexperience;
  * clusters: starters out beyond the first in the OL, secondary and WR rooms;
  * starters returning from absence, and starters new to the team.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque

from betdata.nfl.features.state import Rolling, advanced
from betdata.nfl.points import PREKICK
from betdata.nfl.store import AsOf, to_ns

SLOTS = {"QB": 1, "OL": 5, "WR": 3, "TE": 1, "RB": 1, "DL": 4, "LB": 3, "CB": 3, "S": 2}
NON_QB = [g for g in SLOTS if g != "QB"]
OFFENSE = {"QB", "OL", "WR", "TE", "RB"}
ROLE_GAMES = 4  # player's last games played that define his role
TEAM_GAMES = 4  # team's last games that define its recent strength
STARTER = 0.6  # role that makes a player a starter (clusters, returns, new arrivals)
CANDIDATE = 0.25  # role below which a player can't move a group
QB_PRIOR_DROPBACKS = 150.0
QB_HALF_LIFE_DAYS = 365.0
DROPBACKS_PER_GAME = 38.0
DEFAULT_QB_REPLACEMENT = -0.10  # EPA per dropback; replaced by the training-season estimate
PRACTICE = {
    "Did Not Participate In Practice": "DNP",
    "Limited Participation in Practice": "LP",
    "Full Participation in Practice": "FP",
    "Out (Definitely Will Not Play)": "DNP",
}
PRESENT = {"ACT", "INA"}  # on the team that week (INA: inactive for that game only)
DEFAULT_PLAY_PROB = {"pk_act": 0.97, "pk_out": 0.0, "pk_gone": 0.0, "fr_reserve": 0.02,
                     "fr_gone": 0.05, "fr": 0.95}  # fmt: skip
NS_PER_DAY = 86400 * 10**9


# --- rolling state ---------------------------------------------------------------------------


class Roles(Rolling):
    """Snap shares per player (last games played) and per team (last games)."""

    frame = "snaps"

    def __init__(self):
        super().__init__()
        self.player: dict[str, deque] = defaultdict(lambda: deque(maxlen=ROLE_GAMES))
        self.player_team: dict[str, int] = {}
        # team -> game_id -> players; kickoffs kept to drop the oldest game
        self.team: dict[int, dict[int, dict[str, tuple[str, float]]]] = defaultdict(dict)
        self.kick: dict[int, int] = {}

    def step(self, r) -> None:
        if r.share <= 0:
            return
        self.player[r.key].append((r.game_id, r.group, float(r.share)))
        self.player_team[r.key] = r.team
        games = self.team[r.team]
        games.setdefault(r.game_id, {})[r.key] = (r.group, float(r.share))
        self.kick[r.game_id] = to_ns(r.kickoff)
        if len(games) > TEAM_GAMES:
            del games[min(games, key=self.kick.__getitem__)]

    def role(self, key: str) -> tuple[str | None, float]:
        rows = self.player.get(key)
        if not rows:
            return None, 0.0
        return rows[-1][1], sum(s for _, _, s in rows) / len(rows)


class Rosters(Rolling):
    frame = "rosters"

    def __init__(self):
        super().__init__()
        self.weeks: dict[tuple[int, int], dict[int, dict[str, tuple[str, str | None]]]] = (
            defaultdict(dict)
        )

    def step(self, r) -> None:
        self.weeks[(r.season, r.team)].setdefault(r.week, {})[r.key] = (r.status, r.group)

    def latest(self, season: int, team: int, week: int) -> tuple[int | None, dict]:
        weeks = self.weeks.get((season, team), {})
        known = [w for w in weeks if w <= week]
        if not known:
            return None, {}
        w = max(known)
        return w, weeks[w]


def _text(x) -> str | None:
    return x if isinstance(x, str) and x.strip() else None


class Reports(Rolling):
    frame = "injuries"

    def __init__(self):
        super().__init__()
        self.week: dict[tuple[int, int, int], dict[str, tuple]] = defaultdict(dict)

    def step(self, r) -> None:
        self.week[(r.season, r.week, r.team)][r.key] = (
            _text(r.report_status), _text(r.practice_status), _text(r.group)
        )


class QBs(Rolling):
    """Recency-weighted dropback EPA and starts per quarterback."""

    frame = "player_games"

    def __init__(self):
        super().__init__()
        self.epa: dict[str, float] = defaultdict(float)
        self.db: dict[str, float] = defaultdict(float)
        self.last: dict[str, int] = {}
        self.starts: dict[str, int] = defaultdict(int)

    def step(self, r) -> None:
        if not r.dropbacks:
            return
        t = to_ns(r.kickoff)
        if r.key in self.last:
            d = 0.5 ** ((t - self.last[r.key]) / NS_PER_DAY / QB_HALF_LIFE_DAYS)
            self.epa[r.key] *= d
            self.db[r.key] *= d
        self.last[r.key] = max(t, self.last.get(r.key, t))
        self.epa[r.key] += float(r.dropback_epa)
        self.db[r.key] += float(r.dropbacks)
        if r.dropbacks >= 15:
            self.starts[r.key] += 1

    def value(self, key: str, t_ns: int, repl: float) -> float:
        """EPA per game above a replacement QB."""
        if key not in self.last:
            return 0.0
        d = 0.5 ** ((t_ns - self.last[key]) / NS_PER_DAY / QB_HALF_LIFE_DAYS)
        per_db = (self.epa[key] * d + QB_PRIOR_DROPBACKS * repl) / (
            self.db[key] * d + QB_PRIOR_DROPBACKS
        )
        return DROPBACKS_PER_GAME * (per_db - repl)


def states(a: AsOf):
    return (advanced(a, "av_roles", Roles), advanced(a, "av_rosters", Rosters),
            advanced(a, "av_reports", Reports), advanced(a, "av_qbs", QBs))  # fmt: skip


# --- who might play, and how likely ----------------------------------------------------------


def status_key(point: str, week_status: str | None, has_week_roster: bool, prior: str | None,
               on_prior: bool, report: tuple | None) -> str:  # fmt: skip
    """The bucket a player's chance of playing is learned for."""
    if point == PREKICK and has_week_roster:
        if week_status is None:
            return "pk_gone"
        if week_status != "ACT":
            return "pk_out"
        rs = report[0] if report and report[0] else "-"
        return f"pk_act|{rs}"
    if report:
        rs, ps = report[0] or "-", PRACTICE.get(report[1] or "", "-")
        return f"fr|{rs}|{ps}"
    if prior is not None and prior not in PRESENT:
        return "fr_reserve"
    if not on_prior:
        return "fr_gone"
    return "fr|-|-"


def play_prob(params: dict, key: str) -> float:
    table = params.get("play_prob", {})
    if key in table:
        return table[key]
    return DEFAULT_PLAY_PROB.get(key.split("|")[0], 0.95)


def candidates(a: AsOf, g, team: int, point: str):
    """[(player key, group, role, status bucket)] for one team at a.t."""
    roles, rosters, reports, _ = states(a)
    week_no, week_roster = rosters.latest(g.season, team, g.week)
    has_week = week_no == g.week  # at prekick: the game-day roster; week 1: the final cut
    if point == PREKICK and has_week:
        prior_no, prior = rosters.latest(g.season, team, g.week - 1)
    else:
        prior_no, prior = week_no, week_roster
    report = reports.week.get((g.season, g.week, team), {})
    keys = set(report) | {k for k, (st, _) in week_roster.items() if st in PRESENT}
    for game in roles.team.get(team, {}).values():
        keys |= set(game)
    out = []
    for k in keys:
        group, role = roles.role(k)
        if group is None:
            group = (report.get(k) or (None, None, None))[2] or (week_roster.get(k) or
                                                                  (None, None))[1]  # fmt: skip
        if group not in SLOTS:
            continue
        prior_status = (prior or {}).get(k, (None,))[0]
        on_prior = prior_no is None or k in (prior or {})
        week_status = (week_roster.get(k) or (None,))[0] if has_week else None
        bucket = status_key(point, week_status, has_week, prior_status, on_prior,
                            report.get(k))  # fmt: skip
        out.append((k, group, role, bucket))
    return out


# --- features ------------------------------------------------------------------------------


def top(values: list[float], n: int) -> float:
    return sum(sorted(values, reverse=True)[:n])


def team_features(a: AsOf, g, team: int, point: str) -> dict[str, float]:
    roles, _, _, qbs = states(a)
    params = a.store.params
    repl = params.get("qb_repl", DEFAULT_QB_REPLACEMENT)
    cands = candidates(a, g, team, point)
    recent_games = list(roles.team.get(team, {}).values())
    last_game = recent_games[-1] if recent_games else {}
    out: dict[str, float] = {}

    for grp in NON_QB:
        exp = [play_prob(params, b) * role for k, gr, role, b in cands if gr == grp]
        rec = [top([s for gg, s in game.values() if gg == grp], SLOTS[grp]) for game in
               recent_games]  # fmt: skip
        recent = sum(rec) / len(rec) if rec else top(exp, SLOTS[grp])
        out[grp] = top(exp, SLOTS[grp]) - recent

    starters = [(k, gr, role, play_prob(params, b)) for k, gr, role, b in cands if role >= STARTER]
    out_n = defaultdict(int)
    for _, gr, _, p in starters:
        if p < 0.5:
            out_n["DB" if gr in ("CB", "S") else gr] += 1
    out["cluster_ol"] = max(0, out_n["OL"] - 1)
    out["cluster_db"] = max(0, out_n["DB"] - 1)
    out["cluster_wr"] = max(0, out_n["WR"] - 1)
    out["returning"] = sum(
        1 for k, gr, _, p in starters if p >= 0.5 and gr != "QB" and last_game
        and k not in last_game
    )  # fmt: skip
    out["new"] = sum(1 for k, gr, _, p in starters
                     if p >= 0.5 and roles.player_team.get(k) not in (team, None))  # fmt: skip

    # Quarterbacks: starter = the QB with the biggest role among candidates, then the backup.
    qbc = sorted(((role, qbs.starts.get(k, 0), k, play_prob(params, b))
                  for k, gr, role, b in cands if gr == "QB"), reverse=True)  # fmt: skip
    vals = [(qbs.value(k, a.t_ns, repl), p, qbs.starts.get(k, 0)) for _, _, k, p in qbc]
    if not vals:
        vals = [(0.0, 1.0, 0)]
    v1, p1, _ = vals[0]
    v2, p2, s2 = vals[1] if len(vals) > 1 else (0.0, 1.0, 0)
    expected = p1 * v1 + (1 - p1) * p2 * v2
    recent_qb = [sum(s * qbs.value(k, a.t_ns, repl) for k, (gr, s) in game.items() if gr == "QB")
                 for game in recent_games]  # fmt: skip
    out["qb_value"] = expected
    out["qb_change"] = expected - (sum(recent_qb) / len(recent_qb) if recent_qb else expected)
    out["qb_backup_inexp"] = (1 - p1) * math.exp(-s2 / 4)
    return out


class Availability:
    name = "avail"
    version = 3

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        h = team_features(a, g, g.home, point)
        w = team_features(a, g, g.away, point)
        out = {}
        for k in h:
            out[f"m_avail_{k}"] = h[k] - w[k]
            out[f"t_avail_{k}"] = h[k] + w[k]
        return out


# --- fitted on training seasons ---------------------------------------------------------------


def learn_play_prob(store, seasons, points=("final_report", PREKICK), prior: float = 20.0):
    """P(plays | status bucket) from training seasons: did a candidate with a real role log
    any snaps in the game? Also the replacement-level QB (EPA per dropback of QBs who were not
    their team's main passer). Returns params for store.with_params(**...)."""
    from betdata.nfl.points import point_time

    g = store.games
    snaps = store.frame("snaps").df
    played = {(r.game_id, r.key) for r in snaps[snaps.share > 0].itertuples(index=False)}
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    ids = [i for i in g.index if g.at[i, "season"] in set(seasons)]
    tasks = sorted((point_time(p, g.at[i, "kickoff"]), i, p) for i in ids for p in points)
    for t, gid, p in tasks:
        game = g.loc[gid]
        a = AsOf(store, t)
        for team in (game.home, game.away):
            for k, _, role, bucket in candidates(a, game, team, p):
                if role >= CANDIDATE:
                    c = counts[bucket]
                    c[0] += (gid, k) in played
                    c[1] += 1
    base = {b: DEFAULT_PLAY_PROB.get(b.split("|")[0], 0.95) for b in counts}
    table = {b: round((n_play + prior * base[b]) / (n + prior), 4)
             for b, (n_play, n) in counts.items()}  # fmt: skip

    pg = store.frame("player_games").df
    pg = pg[pg.season.isin(list(seasons)) & (pg.dropbacks > 0)]
    main = pg.loc[pg.groupby(["game_id", "team"])["dropbacks"].idxmax()]
    backups = pg[~pg.index.isin(main.index) & (pg.dropbacks >= 5)]
    repl = float(backups.dropback_epa.sum() / backups.dropbacks.sum()) if len(backups) else (
        DEFAULT_QB_REPLACEMENT
    )
    return {"play_prob": table, "play_counts": {b: c[1] for b, c in counts.items()},
            "qb_repl": round(repl, 4)}  # fmt: skip
