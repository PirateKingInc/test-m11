"""The Action card (owner decision 2026-11-12, MODEL.md "Action card"; config/cards.yaml
`action`): the best picks OG did NOT make official, every NBA card day from Nov 13, under their
own clearly unofficial label and record. No official card changes.

Chosen right after OG Ranking v2 and the Top 3 shadow, at the same moment (`alerts/card.py`
`run_shadow`): from every OG candidate (F / C-NBA picks for the day's games, created by then,
still Live, any market) not on the official OG card and whose player-game-market is not on it,
one pick per player-game-market, ranked by F's calibrated probability (ties: shorter price,
earlier created_at), top 3, 0.25u flat, no labels, each with every rule it failed (Rules v2,
then Ranking v2), the first shown as its reason tag.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

CARD = "action"
SIZE = 3
STAKE = 0.25
START = date(2026, 11, 13)
SOURCE = "action"
FIELDS = ("pick_id", "sport", "card_date", "action", "action_stake", "selected_at", "rank",
          "market", "player", "fair_prob", "fair_prob_cal", "price", "reason",
          "reasons")  # fmt: skip
# the reason tags, in the order shown (Rules v2, then Ranking v2)
REBOUNDS = "rebounds"
NON_ASSIST = "non-assist market"
UNDER_55 = "under 5.5+ assists"
TEAMMATES = "teammates out"
NO_PROB = "no model probability"
PRICE = "outside price range"
NO_MEDIAN = "no median"
MEDIAN = "median wrong side"
DUPLICATE = "duplicate player"
BELOW = "ranked below top 5"
ORDER = (REBOUNDS, NON_ASSIST, UNDER_55, TEAMMATES, NO_PROB, PRICE, NO_MEDIAN, MEDIAN, DUPLICATE,
         BELOW)  # fmt: skip
MARKET_SHORT = {"rebounds": "reb", "assists": "ast", "points": "pts", "threes": "3pm",
                "pra": "pra", "steals": "stl", "blocks": "blk"}


def key(day: date | str) -> str:
    return f"action:nba:{day if isinstance(day, str) else day.isoformat()}"


def active() -> bool:
    from betdata import cards as C

    c = C.load().get(CARD)
    return c is not None and c.active


def applies(day: date) -> bool:
    return day >= START and active()


def pgm(game_id, player, market) -> tuple:
    """Player-game-market: never twice across OG and Action, nor within Action."""
    return (int(game_id), (player or "").lower(), market)


def rules_v2_reasons(a, m=None) -> list[str]:
    """Every OG Rules v2 rule the pick fails (betdata/eligibility.py, applied rule by rule)."""
    from betdata import eligibility as EL

    out = []
    if a.market == "rebounds":
        out.append(REBOUNDS)
    elif a.market != "assists":
        out.append(NON_ASSIST)
    if a.market == "assists" and a.side == "under":
        if a.line is not None and a.line >= EL.UNDER_LINE - 1e-9:
            out.append(UNDER_55)
        tm = None
        try:
            from betdata.alerts.grade import utc

            tm = EL.teammates_out(m, a.game_id, (a.details or {}).get("team_id"),
                                  a.player_name, utc(a.created_at))  # fmt: skip
        except Exception:  # noqa: BLE001 - unknown: the rule cannot apply
            tm = None
        if tm is not None and tm >= EL.TEAMMATES_MAX:
            out.append(TEAMMATES)
    return out


def ranking_v2_reasons(a) -> list[str]:
    """Every Ranking v2 price / median rule the pick fails (betdata/og_ranking.py)."""
    from betdata import og_ranking as OR

    out = []
    if a.fair_prob is None:
        return [NO_PROB]
    if a.price is None or not OR.PRICE_MIN <= int(a.price) <= OR.PRICE_MAX:
        out.append(PRICE)
    med = OR.median_of(a)
    if a.side not in ("over", "under") or med is None or a.line is None:
        out.append(NO_MEDIAN)
    elif (a.side == "over" and not med > a.line) or (a.side == "under" and not med < a.line):
        out.append(MEDIAN)
    return out


def candidates(p, day: date, now: datetime, og: dict, m=None) -> tuple[list, int]:
    """(Action candidates with their reasons, OG candidates seen): every OG-signal pick for the
    day's games created by `now`, not started, Live at `now`, not on the OG card and not a
    player-game-market on it."""
    from betdata import og_ranking as OR
    from betdata.alerts.card import V1_SIGNALS, day_picks
    from betdata.alerts.grade import utc
    from betdata.alerts.labels import available

    on_og = {x["dedup_key"] for x in og.get("picks", [])}
    rejected = og.get("og_rejected") or {}
    pool = [a for a in day_picks(p, "nba", day, V1_SIGNALS)
            if utc(a.created_at) <= now and a.tip is not None and a.tip > now]  # fmt: skip
    og_pgm = {pgm(a.game_id, a.player_name, a.market) for a in pool if a.dedup_key in on_og}
    out, seen = [], 0
    for a in pool:
        if available(p, a, now) is not True:
            continue
        seen += 1
        if a.dedup_key in on_og or pgm(a.game_id, a.player_name, a.market) in og_pgm:
            continue
        why = rules_v2_reasons(a, m) + ranking_v2_reasons(a)
        if not why:  # eligible for OG, but not on its card
            why = [DUPLICATE if rejected.get(a.dedup_key) == OR.DUPLICATE else BELOW]
        a.reasons = [r for r in ORDER if r in why]
        out.append(a)
    return out, seen


def _key(a, fit):
    from betdata.alerts.grade import utc
    from betdata.alerts.rules import decimal
    from betdata.f_calibration import calibrate

    p = -1.0 if a.fair_prob is None else calibrate(a.fair_prob, fit)
    return (-p, decimal(int(a.price)) if a.price else 99.0, utc(a.created_at))


def choose(p, day: date, now: datetime, og: dict, m=None) -> dict:
    """The Action card at `now` (pure on the poller database; `og`: the OG card just chosen)."""
    from functools import partial

    from betdata import og_ranking as OR
    from betdata.f_calibration import calibrate

    fit = OR.f_fit(day)
    cands, seen = candidates(p, day, now, og, m)
    k = partial(_key, fit=fit)
    best: dict = {}
    for a in sorted(cands, key=k):  # one per player-game-market: the best by the key
        best.setdefault(pgm(a.game_id, a.player_name, a.market), a)
    ranked = sorted(best.values(), key=k)
    picks = [{"dedup_key": a.dedup_key, "label": "", "stake": STAKE,
              "fair_prob": None if a.fair_prob is None else round(float(a.fair_prob), 4),
              "fair_prob_cal": None if a.fair_prob is None
              else round(calibrate(a.fair_prob, fit), 4),
              "price": int(a.price), "market": a.market, "player": a.player_name,
              "side": a.side, "line": a.line, "reasons": a.reasons, "reason": a.reasons[0]}
             for a in ranked[:SIZE]]  # fmt: skip
    counts: dict = {}
    for a in cands:
        counts[a.reasons[0]] = counts.get(a.reasons[0], 0) + 1
    out = {"sport": "nba", "day": day.isoformat(), "selected_at": now.isoformat(),
           "source": SOURCE, "picks": picks, "candidates": len(ranked), "seen": seen,
           "reasons": counts}
    if og.get("no_lines"):
        out["no_lines"] = True
    if fit is not None:
        out["f_fit"] = fit.fit_date
    return out


# --- the ledger ------------------------------------------------------------------------------


def ledger_rows(p, out: Path, now: datetime) -> int:
    """Write-once rows of ledger/action_card.csv (one per Action pick, referencing the existing
    pick row) and a card-day row (kind `action`, also when empty)."""
    from sqlalchemy import select

    from betdata.alerts.db import state
    from betdata.ledger import CARD_DAY_FIELDS, _num, append_rows, pick_id

    rows, days = [], []
    for st in p.execute(select(state).where(state.c.key.like("action:%"))):
        c = st.value or {}
        if not c.get("day"):
            continue
        for i, x in enumerate(c.get("picks", []), 1):
            rows.append({"pick_id": pick_id(x["dedup_key"]), "sport": "nba",
                         "card_date": c["day"], "action": "yes",
                         "action_stake": _num(x.get("stake", STAKE)),
                         "selected_at": c.get("selected_at", ""), "rank": str(i),
                         "market": x.get("market", ""), "player": x.get("player") or "",
                         "fair_prob": _num(x.get("fair_prob")),
                         "fair_prob_cal": _num(x.get("fair_prob_cal")),
                         "price": x.get("price", ""), "reason": x.get("reason", ""),
                         "reasons": "; ".join(x.get("reasons", []))})  # fmt: skip
        days.append({"key": f"nba:{c['day']}:{CARD}", "sport": "nba", "card_date": c["day"],
                     "kind": CARD, "selected_at": c.get("selected_at", ""),
                     "picks": str(len(c.get("picks", [])))})  # fmt: skip
    n = append_rows(out / "action_card.csv", FIELDS, rows)
    append_rows(out / "card_days.csv", CARD_DAY_FIELDS, days, key="key")
    return n


def rows_of(out: Path) -> dict[str, dict]:
    from betdata.ledger import read_rows

    return {r["pick_id"]: r for r in read_rows(out / "action_card.csv")}


# --- display -------------------------------------------------------------------------------


def pick_line(x: dict) -> str:
    """'Ivica Zubac u9.5 reb +116 · 0.25u · missed: rebounds'."""
    from betdata.alerts.notify import _fmt_price

    side = {"over": "o", "under": "u"}.get(x.get("side") or "", (x.get("side") or "") + " ")
    line = "" if x.get("line") is None else f"{float(x['line']):g}"
    mk = MARKET_SHORT.get(x.get("market") or "", x.get("market") or "")
    return (f"{x.get('player') or '?'} {side}{line} {mk} {_fmt_price(int(x['price']))} · "
            f"{float(x.get('stake', STAKE)):g}u · missed: {x.get('reason', '?')}")


def push_lines(card: dict | None) -> list[str]:
    """The push section after Spread picks (owner 2026-11-12)."""
    if not active():
        return []
    if card is None:
        return ["Action picks: chosen with the OG card"]
    if not card.get("picks"):
        return ["Action picks: none today"]
    out = ["Action picks (not official):", *[f"  {pick_line(x)}" for x in card["picks"]]]
    why = note(card)  # a short card says why (owner 2026-11-12)
    return out + ([f"  {why.split(': ', 1)[1]}"] if why else [])


def note(card: dict | None) -> str:
    """Tonight's card line of a short or empty Action card ('' when full)."""
    if card is None:
        return ""
    n = len(card.get("picks", []))
    if n >= SIZE:
        return ""
    if card.get("no_lines"):
        return "Action: no picks — no lines posted in time"
    c = card.get("candidates", 0)
    if n == 0:
        return "Action: no picks — every OG candidate is on the OG card" if card.get("seen") \
            else "Action: no picks — no OG candidates today"
    return f"Action: {n} pick{'s' if n != 1 else ''} — only {c} candidate{'s' if c != 1 else ''}" \
        " off the OG card"


# --- the record (graded only, 0.25u) --------------------------------------------------------


def breakdown(rows: list[dict], by: str) -> dict:
    """{tag: Rec} of the Action card's graded picks by reason tag (`reason`) or `market`; the
    parts sum to the whole record."""
    from betdata import official as O

    groups: dict = {}
    for r in O.graded(rows):
        k = r.get("action_reason") if by == "reason" else r.get("market")
        groups.setdefault(k or "?", []).append(r)
    return {k: O.record(v, O.official_stake)
            for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}  # fmt: skip
