"""Why an NBA card is short or empty (owner decision 2026-11-12, MODEL.md "Empty and short card
reasons"): one line per card, built from stored data, for the push and each card's view. The
generic `alerts/card.card_note` text (pinned with Next Gen's selection) stays where no specific
reason is stored, e.g. a card chosen before these reasons existed or with no slate size.

    OG: "no picks — 2-game slate; 7 candidates: 4 rebounds, 2 under 5.5+ assists, 1 outside
        price range"
    Next Gen: "no picks — no edge found on 4 games"
The card name is added by the caller ("OG: ...", or the push section it sits in)."""

from __future__ import annotations

from datetime import date, datetime

OG_SIZE = 5
NG_SIZE = 5
# Rules v2 (eligibility.py) and Ranking v2 (og_ranking.py) reasons -> the short tags
TAGS = {"filtered: rebounds": "rebounds", "filtered: rebound unders": "rebound unders",
        "filtered: non-assist prop": "non-assist market",
        "filtered: not assists (OG)": "non-assist market",
        "filtered: under 5.5+ assists": "under 5.5+ assists",
        "filtered: teammates out": "teammates out",
        "og: price outside -150..+130": "outside price range",
        "og: no median": "no median", "og: median not on the bet's side": "median wrong side",
        "og: duplicate player": "duplicate player", "og: outside top 5": "ranked below top 5",
        "og: no model probability": "no model probability"}  # fmt: skip


def slate(m, day: date) -> int | None:
    """The number of NBA games (not postponed or cancelled) on the Eastern day."""
    from sqlalchemy import func, select

    from betdata.alerts.card import day_bounds
    from betdata.db.schema import games, sports

    if m is None:
        return None
    lo, hi = day_bounds(day)
    q = (select(func.count()).select_from(games)
         .join(sports, sports.c.sport_id == games.c.sport_id)
         .where(sports.c.key == "nba", games.c.start_time_utc >= lo,
                games.c.start_time_utc < hi,
                games.c.status.not_in(("postponed", "cancelled"))))  # fmt: skip
    return m.execute(q).scalar()


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _head(n: int) -> str:
    return "no picks" if n == 0 else _count(n, "pick")


def _slate(s: int | None) -> str:
    return "" if not s else f"{s}-game slate; "


def og(card: dict | None, s: int | None) -> str:
    """OG's line ('' when full, or when no specific reason is stored: a pre-Ranking v2 card)."""
    if not card:
        return ""
    n = len(card.get("picks", []))
    if n >= OG_SIZE or card.get("no_lines"):  # "No lines posted in time" is already specific
        return ""
    if card.get("ranking") != "v2":
        return ""
    reasons: dict = {}
    for why in [*(card.get("filtered") or {}).values(), *(card.get("og_rejected") or {}).values()]:
        t = TAGS.get(why, why)
        reasons[t] = reasons.get(t, 0) + 1
    total = sum(reasons.values()) + n
    if total == n:
        return f"{_head(n)} — {_slate(s)}" + ("no OG model picks for today's games"
                                              if n == 0 else "no other OG model picks")
    parts = ", ".join(f"{v} {k}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))
    return f"{_head(n)} — {_slate(s)}{_count(total, 'candidate')}: {parts}"


def nextgen(p, card: dict | None, s: int | None) -> str:
    """Next Gen's line ('' when full or when the slate size is unknown)."""
    from betdata.alerts.card import day_picks
    from betdata.alerts.grade import utc

    if not card:
        return ""
    n = len(card.get("picks", []))
    if n >= NG_SIZE or card.get("no_lines"):  # "No lines posted in time" is already specific
        return ""
    if not s:
        return ""
    day = date.fromisoformat(card["day"])
    t = datetime.fromisoformat(card["selected_at"])
    cands = [a for a in day_picks(p, "nba", day, ("engine",)) if utc(a.created_at) <= t]
    if not cands:
        return f"{_head(n)} — no edge found on {_count(s, 'game')}"
    filtered: dict = {}
    for why in (card.get("filtered") or {}).values():
        k = TAGS.get(why, why)
        filtered[k] = filtered.get(k, 0) + 1
    rest = len(cands) - n - sum(filtered.values())
    parts = [f"{v} {k}" for k, v in sorted(filtered.items(), key=lambda kv: -kv[1])]
    if rest > 0:
        parts.append(f"{rest} below the bar (EV, long-shot gate or no longer Live)")
    return f"{_head(n)} — {_slate(s)}{_count(len(cands), 'engine pick')}: {', '.join(parts)}"


def action(card: dict | None) -> str:
    """The Action card's line (betdata/action.py `note`, without its name)."""
    from betdata import action as AC

    t = AC.note(card)
    return t.split(": ", 1)[1] if t.startswith("Action: ") else t
