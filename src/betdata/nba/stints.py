"""NBA lineup stints from play-by-play: stretches of a game with the same ten players on the floor.

Plays come from sportsdataverse's season files (history) or ESPN's game summary (live); both are
ESPN's play-by-play, reduced to `Play` tuples. Players are ESPN athlete ids.

Rules (also in MODEL.md, "M15 NBA lineup stints"):
* **Period 1** starts with the box score's five starters per team.
* **Later periods** list nobody, so the five are inferred: a player who records any event in the
  period (technicals and ejections excepted: bench players get those) or is subbed out before
  being subbed in was on the floor at its start. If fewer than five are found, the rest come from
  the previous period's closing lineup, leaving out anyone subbed in during this period; this
  fills only when exactly the missing number of such players is left. Otherwise, or if a team's
  starting five can't be found, or a substitution names a player who isn't on (or is already on)
  the floor, the period is *unresolved* and its stints are skipped.
* **Same-clock dead balls:** all substitutions made at one game-clock instant are applied
  together, after the other plays at that instant (free throws around them belong to the lineup
  of the foul). A player subbed in and out in the same dead ball never appears.
* **Points** are the changes in the running score, credited to the lineup on the floor.
* **Possessions** per team: FGA - offensive rebounds by players + turnovers + 0.44 x FTA
  (technical free throws excluded), counted from the play types (travels are turnovers; team
  turnovers count, team rebounds don't).
* **Time** is seconds elapsed in the game (periods of 720 s, overtimes of 300 s).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import NamedTuple

PERIOD = 720.0
OVERTIME = 300.0
NO_INFERENCE = ("Technical", "Ejection")  # a bench player can be named on these


class Play(NamedTuple):
    seq: int
    period: int
    clock: float  # seconds left in the period
    kind: str  # ESPN type text
    team: str | None  # ESPN team id of the play
    athletes: tuple[str, ...]  # substitutions: (entering, leaving)
    home_score: int | None  # running score, ESPN's home and away teams
    away_score: int | None
    shooting: bool


@dataclass
class StintResult:
    stints: list[dict] = field(default_factory=list)
    unresolved: list[tuple[int, str]] = field(default_factory=list)  # (period, reason)
    periods: int = 0


def clock_seconds(text) -> float:
    """'7:21', '0:32.1', '32.1' (ESPN's live clock under a minute) -> seconds."""
    parts = str(text or "0").strip().split(":")
    if len(parts) == 3:  # '1:38:59' (a few 2025-26 rows) is 1:38.59
        return int(parts[0]) * 60 + float(f"{parts[1]}.{parts[2]}")
    if len(parts) == 2:
        return int(parts[0] or 0) * 60 + float(parts[1] or 0)
    return float(parts[0])


def period_start(period: int) -> float:
    return PERIOD * (period - 1) if period <= 4 else 4 * PERIOD + OVERTIME * (period - 5)


def period_length(period: int) -> float:
    return PERIOD if period <= 4 else OVERTIME


def is_sub(p: Play) -> bool:
    return p.kind == "Substitution" and len(p.athletes) >= 2


def possessions(p: Play) -> float:
    """This play's share of its team's possession estimate (FGA - OREB + TOV + 0.44 FTA)."""
    k = p.kind
    if k.startswith("Free Throw"):
        return 0.0 if "Technical" in k else 0.44
    if p.shooting:
        return 1.0
    if k == "Offensive Rebound":
        return -1.0 if p.athletes else 0.0  # team rebounds aren't in the box-score formula
    if ("Turnover" in k and k != "No Turnover") or k == "Traveling":  # no separate row
        return 1.0
    return 0.0


def _groups(plays: list[Play]):
    """Plays at one clock instant, other plays first, then that instant's substitutions."""
    i = 0
    while i < len(plays):
        j = i
        while j < len(plays) and plays[j].clock == plays[i].clock:
            j += 1
        block = plays[i:j]
        yield [p for p in block if not is_sub(p)], [p for p in block if is_sub(p)]
        i = j


class _Sides:
    """Which side (home/away in our game's orientation) a player or ESPN team is on."""

    def __init__(self, plays: list[Play], roster: dict[str, str]):
        self.roster = dict(roster)
        votes: dict[str, Counter] = {}
        for p in plays:
            if p.team and p.athletes and p.athletes[0] in roster and not is_sub(p):
                votes.setdefault(p.team, Counter())[roster[p.athletes[0]]] += 1
            if p.team and is_sub(p):
                for a in p.athletes[:2]:
                    if a in roster:
                        votes.setdefault(p.team, Counter())[roster[a]] += 1
        self.team = {t: c.most_common(1)[0][0] for t, c in votes.items()}
        for p in plays:  # players missing from the box score: their play's team
            side = self.team.get(p.team or "")
            if side and p.athletes:
                for a in p.athletes[:2] if is_sub(p) else p.athletes[:1]:
                    self.roster.setdefault(a, side)

    def of(self, athlete: str) -> str | None:
        return self.roster.get(athlete)


def _infer_start(plays: list[Play], sides: _Sides, side: str, previous: set[str] | None,
                 *, fill: bool = True) -> set[str] | None:  # fmt: skip
    found: list[str] = []
    came_in: set[str] = set()
    for p in plays:  # in play order: who acted, or left, before entering
        if is_sub(p):
            enters, leaves = p.athletes[0], p.athletes[1]
            if sides.of(leaves) == side and leaves not in came_in and leaves not in found:
                found.append(leaves)
            if sides.of(enters) == side and enters not in found:
                came_in.add(enters)
            continue
        if any(w in p.kind for w in NO_INFERENCE) and "3-Seconds" not in p.kind:
            continue
        for a in p.athletes:
            if sides.of(a) == side and a not in came_in and a not in found:
                found.append(a)
    if len(found) > 5:
        return None
    if len(found) < 5:
        if not fill or previous is None:
            return None
        rest = [a for a in sorted(previous) if a not in found and a not in came_in]
        if len(rest) != 5 - len(found):
            return None
        found += rest
    return set(found)


def _open(stints: list[dict], period: int, t: float, lineup: dict) -> None:
    """A new stint from `t` with the current lineup (or, at the same instant, the last one's
    lineup replaced: one stint per instant)."""
    if stints and stints[-1]["start_sec"] == t:
        stints[-1].update(home=set(lineup["home"]), away=set(lineup["away"]))
        return
    if stints:
        stints[-1]["end_sec"] = t
    stints.append(dict(period=period, start_sec=t, end_sec=None, home=set(lineup["home"]),
                       away=set(lineup["away"]), home_pts=0, away_pts=0, home_poss=0.0,
                       away_poss=0.0))  # fmt: skip


def build_stints(plays: list[Play], roster: dict[str, str], starters: set[str],
                 espn_home: str | None) -> StintResult:  # fmt: skip
    """Stints of one game. `roster`: athlete id -> 'home'/'away' in our game's orientation (the
    box score); `starters`: the box score's starters; `espn_home`: ESPN's home team id, to
    orient the running score."""
    plays = sorted(plays, key=lambda p: (p.period, -p.clock, p.seq))
    sides = _Sides(plays, roster)
    swapped = sides.team.get(espn_home or "") == "away"
    out = StintResult()
    prev_score = (0, 0)
    previous: dict[str, set[str] | None] = {"home": None, "away": None}
    for period in sorted({p.period for p in plays}):
        out.periods += 1
        pp = [p for p in plays if p.period == period]
        base, length = period_start(period), period_length(period)
        lineup: dict[str, set[str] | None] = {}
        for side in ("home", "away"):
            first = {a for a in starters if sides.of(a) == side} if period == 1 else set()
            lineup[side] = first if len(first) == 5 else _infer_start(
                pp, sides, side, previous[side], fill=period > 1)
        reason = None if all(lineup.values()) else "starting lineup not found"
        stints: list[dict] = []
        if reason is None:
            _open(stints, period, round(base, 1), lineup)
        for others, subs in _groups(pp):
            t = round(base + length - others[0].clock if others else
                      base + length - subs[0].clock, 1)  # fmt: skip
            for p in others:
                if p.home_score is not None and p.away_score is not None:
                    score = (p.home_score, p.away_score)
                    dh, da = score[0] - prev_score[0], score[1] - prev_score[1]
                    prev_score = score
                    if swapped:
                        dh, da = da, dh
                    if stints and reason is None:
                        stints[-1]["home_pts"] += dh
                        stints[-1]["away_pts"] += da
                side = sides.team.get(p.team or "")
                if side and stints and reason is None:
                    stints[-1][f"{side}_poss"] += possessions(p)
            if not subs or reason is not None:
                continue
            for p in subs:
                enters, leaves = p.athletes[0], p.athletes[1]
                side = sides.of(enters) or sides.of(leaves)
                on = lineup.get(side) if side else None
                if on is None or leaves not in on or enters in on:
                    reason = f"substitution at {t:g}s doesn't fit the lineup"
                    break
                on.discard(leaves)
                on.add(enters)
            if reason is None and (lineup["home"] != stints[-1]["home"]
                                   or lineup["away"] != stints[-1]["away"]):  # fmt: skip
                _open(stints, period, t, lineup)
        if reason is not None:
            out.unresolved.append((period, reason))
            previous = {"home": None, "away": None}
            continue
        stints[-1]["end_sec"] = round(base + length, 1)
        if len(stints) > 1 and stints[-1]["start_sec"] == stints[-1]["end_sec"]:
            last = stints.pop()  # subs at the final buzzer: what's left belongs to the stint
            for k in ("home_pts", "away_pts", "home_poss", "away_poss"):
                stints[-1][k] += last[k]
        previous = {"home": set(lineup["home"]), "away": set(lineup["away"])}
        for s in stints:
            s["home_players"] = ",".join(sorted(s.pop("home")))
            s["away_players"] = ",".join(sorted(s.pop("away")))
            s["home_poss"], s["away_poss"] = round(s["home_poss"], 2), round(s["away_poss"], 2)
        out.stints.extend(stints)
    return out

