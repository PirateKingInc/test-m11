"""Early-source claims against the official NBA injury report (owner decision 2026-11-05).

- `official(m, game, player, at)`: the player's latest official status known by `at`.
- `ahead`: a claim counts only when the official report does not show it yet; when it already
  does, the claim is logged as "official first" and ignored.
- `resolve`: once the report catches up, the claim is confirmed (same status) or contradicted
  (the opposite), with the minutes the source was ahead; still not on the report at tip, it is
  "unconfirmed". A Sleeper trending-add spike is a hint only: confirmed when the player's
  official status changes within 6 hours, else "no change"; it never alerts.
- `good_down_to(fair, theta)`: the worst American price that still clears the bar
  (fair x decimal - 1 >= theta).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import select, update

from betdata.news.parse import AVAILABLE, OUT, _words

HINT_WINDOW = timedelta(hours=6)
# official statuses that mean each claim
MEANS = {OUT: {"Out"}, AVAILABLE: {"Available", "Probable"}}
OPPOSITE = {OUT: {"Available", "Probable"}, AVAILABLE: {"Out"}}


def _key(name: str) -> str:
    """'Last, First' (the report) and 'First Last' (posts, the engine) to one key."""
    if "," in (name or ""):
        last, first = name.split(",", 1)
        name = f"{first} {last}"
    return " ".join(_words(name))


def _utc(t):
    from betdata.alerts.grade import utc

    return None if t is None else utc(t)


def official(m, game_id: int, player: str, at: datetime | None = None,
             after: datetime | None = None) -> list[tuple[str, datetime]]:  # fmt: skip
    """The player's official rows for the game, (status, known_at) by time; `at` keeps those
    known by then, `after` those known after."""
    from betdata.db.schema import nba_injury_reports as t

    q = select(t.c.player_name, t.c.status, t.c.known_at).where(t.c.game_id == game_id)
    if at is not None:
        q = q.where(t.c.known_at <= at)
    if after is not None:
        q = q.where(t.c.known_at > after)
    want = _key(player)
    rows = [(r.status, _utc(r.known_at)) for r in m.execute(q.order_by(t.c.known_at))
            if _key(r.player_name) == want]  # fmt: skip
    return rows


def official_now(m, game_id: int, player: str, at: datetime) -> str | None:
    rows = official(m, game_id, player, at=at)
    return rows[-1][0] if rows else None


def shows(status: str, official_status: str | None) -> bool:
    """Does the official report already show the claim? A player never listed is available."""
    if status == AVAILABLE:
        return official_status is None or official_status in MEANS[AVAILABLE] \
            or official_status == "Removed"
    return official_status in MEANS[OUT]


def ahead(m, game_id: int, player: str, status: str, at: datetime) -> tuple[bool, str | None]:
    """(ahead of the official report, the official status at `at`)."""
    cur = official_now(m, game_id, player, at)
    return (not shows(status, cur)), cur


def resolve(p, m, now: datetime, tips: dict[int, datetime]) -> list[int]:
    """Settle pending claims against the official report; returns the claims settled."""
    from betdata.alerts.db import early_claims as ec

    done = []
    for c in p.execute(select(ec).where(ec.c.outcome == "pending")).all():
        posted = _utc(c.posted_at)
        rows = official(m, c.game_id, c.player_name, after=posted) if c.game_id else []
        outcome = official_status = official_at = None
        if c.status is None:  # a Sleeper hint: any status change within the window
            before = official_now(m, c.game_id, c.player_name, posted) if c.game_id else None
            moved = [(s, t) for s, t in rows if s != before and t - posted <= HINT_WINDOW]
            if moved:
                outcome, (official_status, official_at) = "confirmed", moved[0]
            elif now - posted > HINT_WINDOW:
                outcome = "no change"
        else:
            for s, t in rows:
                if s in MEANS[c.status] or (c.status == AVAILABLE and s == "Removed"):
                    outcome, official_status, official_at = "confirmed", s, t
                    break
                if s in OPPOSITE[c.status]:
                    outcome, official_status, official_at = "contradicted", s, t
                    break
            tip = tips.get(c.game_id)
            if outcome is None and tip is not None and now >= tip:
                outcome = "unconfirmed"
        if outcome is None:
            continue
        lead = (None if official_at is None
                else round((official_at - posted).total_seconds() / 60, 1))  # fmt: skip
        p.execute(update(ec).where(ec.c.claim_id == c.claim_id).values(
            outcome=outcome, official_status=official_status, official_at=official_at,
            lead_min=lead, resolved_at=now))  # fmt: skip
        done.append(c.claim_id)
    return done


def american_floor(dec: float) -> int:
    """The worst American price whose decimal is still >= `dec`."""
    if dec >= 2:
        return math.ceil(round((dec - 1) * 100, 6))
    return -math.floor(round(100 / (dec - 1), 6))


def good_down_to(fair: float, theta: float) -> int:
    """The worst price that still clears the bar: fair x decimal - 1 >= theta."""
    return american_floor((1 + theta) / fair)
