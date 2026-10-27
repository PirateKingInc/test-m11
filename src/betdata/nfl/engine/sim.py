"""M17 Part C: simulate a game drive by drive, N times at once (numpy, vectorised over sims).

Each drive: the outcome model gives P(result) from the start field position, the matchup, the
score and the clock; a 2016-2019 drive with that result and start bucket is resampled for its
plays, seconds (x the offence's pace), yards, field goal distance and the field position it
hands over. Its plays split into dropbacks (the engine's pass curve: score, clock, the offence's
pass tendency), sacks, scrambles and attempts, and the drive's yards into passing, scrambling
and designed-run yards by the matchup's yards per attempt and per carry. Kickoffs read the
recent kickoff results (rules change). Overtime: 10 minutes, a touchdown on the first possession
wins, otherwise the first score after both have had the ball.

After the game, each team's attempts, designed runs, yards and touchdowns go to its players:
who plays is drawn from M12's play probabilities; targets and carries are Dirichlet-multinomial
on the shrunk shares; receptions binomial on the catch rate; yards a sum of gamma gains per
catch or carry, rescaled to the team's total (so receivers' yards add up to passing yards);
touchdowns by red-zone shares. Score, script and volume are therefore one correlated draw.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from betdata.nfl.engine import drives as D

IDX = {o: i for i, o in enumerate(D.OUTCOMES)}
SACK_YARDS, SCRAMBLE_YARDS = 6.5, 6.0


@dataclass
class Side:
    """One team's inputs for a game (offence vs that opponent)."""

    x: float  # matchup expected EPA per play (starting QB)
    offset: float  # logit pass tendency vs the league
    pace: float  # seconds-per-play ratio to the league
    sack: float
    scramble: float
    ypa: float
    ypc: float
    pass_td_share: float
    # QBs: (id, p_play, x delta if he plays instead of the expected starter)
    qbs: list[tuple[str, float, float]] = field(default_factory=list)
    # receivers: id, q (chance he misses), target share, catch rate, yards per reception,
    # red-zone target share
    rec: list[tuple[str, float, float, float, float, float]] = field(default_factory=list)
    # rushers (designed runs; QB included): id, q, carry share, yards per carry, rz share
    rush: list[tuple[str, float, float, float, float]] = field(default_factory=list)


@dataclass
class SimParams:
    n: int = 10_000
    alpha_targets: float = 30.0  # Dirichlet concentration of target shares
    alpha_carries: float = 25.0
    kappa_rec: float = 1.2  # gamma shape per reception
    kappa_rush: float = 0.6  # gamma shape per carry
    none_share: float = 0.05  # attempts with no targeted skill player (throwaways, spikes)
    ot_seconds: float = 600.0


@dataclass
class GameSim:
    margin: np.ndarray  # home - away
    home_pts: np.ndarray
    away_pts: np.ndarray
    team: dict  # (side, stat) -> array
    players: dict  # (id, stat) -> array


def _draw_rows(rng, model: D.DriveModel, outcome: np.ndarray, start: np.ndarray) -> np.ndarray:
    """One template drive per sim for its (outcome, start bucket); falls back to neighbouring
    buckets (then any bucket) when a combination never happened in 2016-2019."""
    b = D.bucket(start)
    out = np.full((len(outcome), 7), np.nan)
    keys = outcome * 100 + b
    for key in np.unique(keys):
        idx = np.nonzero(keys == key)[0]
        o, bb = D.OUTCOMES[key // 100], key % 100
        arr = None
        for d in (0, 1, -1, 2, -2, 3, -3):
            arr = model.templates.get(f"{o}|{bb + d}")
            if arr is not None and len(arr):
                break
        if arr is None or not len(arr):
            arr = np.vstack([v for k, v in model.templates.items() if k.startswith(o + "|")])
        out[idx] = arr[rng.integers(0, len(arr), len(idx))]
    return out


def simulate(model: D.DriveModel, home: Side, away: Side, kick_pool: np.ndarray, pat: dict,
             p: SimParams | None = None, seed: int = 0,
             home_field: bool = True) -> GameSim:  # fmt: skip
    p = p or SimParams()
    rng = np.random.default_rng(seed)
    n = p.n
    sides = (home, away)
    # who plays QB in each sim (first QB who plays, in order)
    x_sim = np.zeros((n, 2))
    qb_id = np.empty((n, 2), dtype=object)
    for s, side in enumerate(sides):
        x_sim[:, s] = side.x
        chosen = np.zeros(n, bool)
        for qid, pp, dx in side.qbs:
            take = ~chosen & (rng.random(n) < pp)
            x_sim[take, s] = side.x + dx
            qb_id[take, s] = qid
            chosen |= take
        if side.qbs:  # nobody drawn: the last listed QB (replacement) plays
            qid, _, dx = side.qbs[-1]
            x_sim[~chosen, s] = side.x + dx
            qb_id[~chosen, s] = qid
    offs = np.array([s.offset for s in sides])
    pace = np.array([s.pace for s in sides])
    sack = np.array([s.sack for s in sides])
    scr = np.array([s.scramble for s in sides])
    ypa = np.array([s.ypa for s in sides])
    ypc = np.array([s.ypc for s in sides])
    ptd = np.array([s.pass_td_share for s in sides])

    score = np.zeros((n, 2))
    stats = {k: np.zeros((n, 2)) for k in ("plays", "dropbacks", "sacks", "scrambles",
                                           "attempts", "runs", "gross_yards", "pass_td",
                                           "rush_td", "fg_made", "drives", "td")}  # fmt: skip
    first = (rng.random(n) < 0.5).astype(int)  # receives the opening kickoff

    def play_half(half: int, receiving: np.ndarray, clock0: float, ot: bool = False):
        poss = receiving.copy()
        start = rng.choice(kick_pool, n)
        clock = np.full(n, clock0)
        active = np.ones(n, bool) if not ot else (score[:, 0] == score[:, 1])
        had = np.zeros((n, 2), bool)
        for _ in range(60):
            if not active.any():
                break
            a = np.nonzero(active)[0]
            ps = poss[a]
            sd = score[a, ps] - score[a, 1 - ps]
            game_sec = clock[a] + (1800 if half == 1 else 0)
            X = D.features(start[a], x_sim[a, ps], sd, clock[a], np.full(len(a), half),
                           game_sec, (ps == 0) & home_field)  # fmt: skip
            pr = model.probs(X)
            u = rng.random(len(a))
            out = (pr.cumsum(1) < u[:, None]).sum(1).clip(0, len(D.OUTCOMES) - 1)
            row = _draw_rows(rng, model, out, start[a])
            plays, secs, yards, fgd, nxt, _share, _st = row.T
            secs = np.nan_to_num(secs, nan=150.0) * pace[ps] * model.time_scale
            # A drive longer than the clock left: a score stands, compressed into the time left
            # (the outcome model already reads the clock, from real two-minute drives); any
            # other result becomes the end of the half.
            short = secs > clock[a]
            scoring = np.isin(out, (IDX["td"], IDX["fg"], IDX["opp_td"]))
            out = np.where(short & ~scoring, IDX["end_half"], out)
            secs = np.minimum(secs, clock[a])
            # play mix
            F = D.pass_features(sd, clock[a], np.full(len(a), half), game_sec, start[a],
                                offs[ps])  # fmt: skip
            pp = model.p_pass(F, out)
            pl = np.maximum(np.nan_to_num(plays, nan=5.0), 0).astype(int)
            db = rng.binomial(pl, pp)
            sk = rng.binomial(db, sack[ps])
            sc = rng.binomial(db - sk, scr[ps])
            at = db - sk - sc
            ru = pl - db
            # yards: the drive's yards (sacks added back); split per team after the game
            gross = np.nan_to_num(yards) + sk * SACK_YARDS
            # points
            td = out == IDX["td"]
            pts = np.zeros(len(a))
            two = td & (rng.random(len(a)) < pat["two_rate"])
            pts += td * 6 + np.where(two, 2 * (rng.random(len(a)) < pat["two_made"]),
                                     td * (rng.random(len(a)) < pat["xp"]))  # fmt: skip
            fg_try = out == IDX["fg"]
            fg_ok = fg_try & (rng.random(len(a)) < model.p_fg(np.nan_to_num(fgd, nan=45.0)))
            pts += fg_ok * 3
            opp = out == IDX["opp_td"]
            saf = out == IDX["safety"]
            score[a, ps] += pts
            score[a, 1 - ps] += opp * 7 + saf * 2
            pass_td = td & (rng.random(len(a)) < ptd[ps])
            for k, v in (("plays", pl), ("dropbacks", db), ("sacks", sk), ("scrambles", sc),
                         ("attempts", at), ("runs", ru), ("gross_yards", gross),
                         ("pass_td", pass_td), ("rush_td", td & ~pass_td),
                         ("fg_made", fg_ok), ("drives", np.ones(len(a))), ("td", td)):
                np.add.at(stats[k], (a, ps), v)
            had[a, ps] = True
            clock[a] -= secs
            # next possession and field position
            kick = rng.choice(kick_pool, len(a))
            to_other = ~opp  # after an opponent TD the scoring defence kicks off to us again
            new_start = np.where(td | fg_ok | opp | saf, kick,
                                 np.where(np.isnan(nxt), 75.0, nxt))  # fmt: skip
            new_start = np.where(saf, 65.0, new_start)
            poss[a] = np.where(to_other, 1 - ps, ps)
            start[a] = np.clip(new_start, 1, 99)
            ended = clock[a] <= 0
            if ot:
                decided = (score[a, 0] != score[a, 1]) & (td | had[a].all(1) | opp | saf)
                ended |= decided
            active[a[ended]] = False

    play_half(1, first, 1800.0)
    play_half(2, 1 - first, 1800.0)
    tied = score[:, 0] == score[:, 1]
    if tied.any():
        play_half(3, (rng.random(n) < 0.5).astype(int), p.ot_seconds, ot=True)

    # A team's yards split once per game by its expected passing, rushing and scrambling
    # gains (per drive this would bias toward runs: incompletions cluster on failed drives).
    e_p = stats["attempts"] * ypa
    e_r = stats["runs"] * ypc
    e_s = stats["scrambles"] * SCRAMBLE_YARDS
    tot = e_p + e_r + e_s
    w = np.where(tot > 0, stats["gross_yards"] / np.where(tot > 0, tot, 1), 0)
    stats["pass_yards"], stats["run_yards"], stats["scramble_yards"] = e_p * w, e_r * w, e_s * w
    players = {}
    for s, side in enumerate(sides):
        _allocate(rng, side, s, stats, qb_id, players, p)
    return GameSim(score[:, 0] - score[:, 1], score[:, 0], score[:, 1],
                   {(s, k): v[:, s] for k, v in stats.items() for s in (0, 1)}, players)


def _gamma_sum(rng, count: np.ndarray, mean: np.ndarray, kappa: float) -> np.ndarray:
    shape = count * kappa
    out = np.zeros(count.shape)
    pos = shape > 0
    out[pos] = rng.gamma(shape[pos], (np.broadcast_to(mean, count.shape)[pos]) / kappa)
    return out


def _shares(rng, base: np.ndarray, active: np.ndarray, alpha: float, extra: float = 0.0):
    """Per-sim shares: Dirichlet around the active players' base shares (renormalised), with an
    optional `extra` bucket (no targeted player) as the last column."""
    w = base[None, :] * active
    tot = w.sum(1, keepdims=True)
    w = np.where(tot > 0, w / np.where(tot > 0, tot, 1), 0) * (1 - extra)
    if extra:
        w = np.hstack([w, np.full((len(w), 1), extra)])
    g = rng.gamma(np.maximum(alpha * w, 1e-9))
    g = np.where(w > 0, g, 0)
    s = g.sum(1, keepdims=True)
    return np.where(s > 0, g / np.where(s > 0, s, 1), 0)


def _allocate(rng, side: Side, s: int, stats: dict, qb_id, players: dict, p: SimParams):
    n = len(qb_id)
    att = stats["attempts"][:, s].astype(int)
    runs = stats["runs"][:, s].astype(int)
    pass_y = stats["pass_yards"][:, s]
    run_y = stats["run_yards"][:, s]
    if side.rec:
        ids = [r[0] for r in side.rec]
        q = np.array([r[1] for r in side.rec])
        act = rng.random((n, len(ids))) >= q
        th = _shares(rng, np.array([r[2] for r in side.rec]), act, p.alpha_targets,
                     p.none_share)  # fmt: skip
        tg = rng.multinomial(att, th)[:, :-1]
        rec = rng.binomial(tg, np.array([r[3] for r in side.rec]))
        raw = _gamma_sum(rng, rec, np.array([r[4] for r in side.rec]), p.kappa_rec)
        tot = raw.sum(1, keepdims=True)
        yds = np.where(tot > 0, raw * (pass_y[:, None] / np.where(tot > 0, tot, 1)), 0)
        rz = np.array([max(r[5], 1e-6) for r in side.rec])[None, :] * act
        rzt = rz.sum(1, keepdims=True)
        td = rng.multinomial(stats["pass_td"][:, s].astype(int),
                             np.where(rzt > 0, rz / np.where(rzt > 0, rzt, 1), 1 / len(ids)))
        for j, pid in enumerate(ids):
            players[(pid, "targets")] = tg[:, j]
            players[(pid, "receptions")] = rec[:, j]
            players[(pid, "rec_yards")] = yds[:, j]
            players[(pid, "rec_td")] = td[:, j]
            players[(pid, "played")] = act[:, j]
    if side.rush:
        ids = [r[0] for r in side.rush]
        q = np.array([r[1] for r in side.rush])
        act = rng.random((n, len(ids))) >= q
        # a QB rusher plays only in the sims where he is the QB
        for j, pid in enumerate(ids):
            if any(pid == qb[0] for qb in side.qbs):
                act[:, j] = qb_id[:, s] == pid
        th = _shares(rng, np.array([r[2] for r in side.rush]), act, p.alpha_carries)
        car = rng.multinomial(runs, np.where(th.sum(1, keepdims=True) > 0, th,
                                             1 / len(ids)))  # fmt: skip
        raw = _gamma_sum(rng, car, np.array([r[3] for r in side.rush]), p.kappa_rush)
        tot = raw.sum(1, keepdims=True)
        yds = np.where(tot > 0, raw * (run_y[:, None] / np.where(tot > 0, tot, 1)), 0)
        rz = np.array([max(r[4], 1e-6) for r in side.rush])[None, :] * act
        rzt = rz.sum(1, keepdims=True)
        td = rng.multinomial(stats["rush_td"][:, s].astype(int),
                             np.where(rzt > 0, rz / np.where(rzt > 0, rzt, 1), 1 / len(ids)))
        for j, pid in enumerate(ids):
            players[(pid, "carries")] = car[:, j]
            players[(pid, "rush_yards")] = yds[:, j] + players.get((pid, "rush_yards"), 0)
            players[(pid, "rush_td")] = td[:, j]
            players.setdefault((pid, "played"), act[:, j])
    for qid, _, _ in side.qbs:
        me = qb_id[:, s] == qid
        players[(qid, "pass_yards")] = np.where(me, stats["pass_yards"][:, s], 0)
        players[(qid, "pass_attempts")] = np.where(me, stats["attempts"][:, s], 0)
        players[(qid, "rush_yards")] = (players.get((qid, "rush_yards"), 0)
                                        + np.where(me, stats["scramble_yards"][:, s], 0))
        players[(qid, "played")] = me


def consistency(gs: GameSim, home: Side, away: Side) -> dict:
    """Part C identity checks, per simulation: the worst violation of each (0 = holds)."""
    out = {}
    for s, side in enumerate((home, away)):
        tag = "home" if s == 0 else "away"
        att = gs.team[(s, "attempts")]
        tg = sum((gs.players.get((r[0], "targets"), 0) for r in side.rec), np.zeros_like(att))
        rec = sum((gs.players.get((r[0], "receptions"), 0) for r in side.rec), np.zeros_like(att))
        ry = sum((gs.players.get((r[0], "rec_yards"), 0) for r in side.rec), np.zeros_like(att))
        qy = sum((gs.players.get((q[0], "pass_yards"), 0) for q in side.qbs), np.zeros_like(att))
        ptd = sum((gs.players.get((r[0], "rec_td"), 0) for r in side.rec), np.zeros_like(att))
        car = sum((gs.players.get((r[0], "carries"), 0) for r in side.rush), np.zeros_like(att))
        out[f"{tag}_targets_over_attempts"] = float(np.maximum(tg - att, 0).max())
        out[f"{tag}_receptions_over_targets"] = float(np.maximum(rec - tg, 0).max())
        has = rec > 0
        out[f"{tag}_rec_yards_vs_pass_yards"] = float(
            np.abs(ry - gs.team[(s, "pass_yards")])[has].max()) if has.any() else 0.0
        if side.qbs:
            out[f"{tag}_qb_yards_vs_pass_yards"] = float(
                np.abs(qy - gs.team[(s, "pass_yards")]).max())
        if side.rec:
            out[f"{tag}_rec_td_vs_pass_td"] = float(np.abs(ptd - gs.team[(s, "pass_td")]).max())
        if side.rush:
            out[f"{tag}_carries_vs_runs"] = float(np.abs(car - gs.team[(s, "runs")]).max())
        out[f"{tag}_td_split"] = float(np.abs(gs.team[(s, "pass_td")] + gs.team[(s, "rush_td")]
                                              - gs.team[(s, "td")]).max())
        pts = gs.home_pts if s == 0 else gs.away_pts
        out[f"{tag}_points_floor"] = float(np.maximum(
            6 * gs.team[(s, "td")] + 3 * gs.team[(s, "fg_made")] - pts, 0).max())
    m = gs.margin
    out["median_inside_p10_p90"] = float(not (np.percentile(m, 10) <= np.median(m)
                                              <= np.percentile(m, 90)))
    return out
