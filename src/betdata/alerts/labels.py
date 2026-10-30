"""Pick labels and stakes (owner request 2026-10-30, MODEL.md "Pick labels and stakes"): an
in-house simulation of a pick service. Display only: no signal, threshold, model or grade
changes.

Every new M18 pick (C, F, G1, G2) gets a label and stake when it is created, stored with the
alert (`details["label"]`) and written once to the ledger (`ledger/labels.csv`):
- **TAKE** (1 unit): edge >= 1.5x the signal's threshold, AND (edge source "news" OR a fresh
  Pinnacle price sides with the pick), AND the price is still available at the latest poll.
- **LEAN** (0.5 unit): clears the threshold and the price is available, but not TAKE. A pick
  whose edge is book disagreement against a two-book consensus is LEAN at most.
- **PASS** (0 units): the price was no longer available at the latest poll (not expected: the
  alert is priced from that poll; recorded so every labelled pick has a stake).
Picks from before labels shipped stay unlabelled (no backfill).

Separately, each page build shows a display status for upcoming picks, never stored:
**Live** (the alerted price, or better, is still up), **Moved** (worse than alerted but still
better than fair) or **Gone** (the book moved past fair value, or the line moved against it).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from betdata.moneyline import implied, no_vig

TAKE, LEAN, PASS = "TAKE", "LEAN", "PASS"
STAKE = {TAKE: 1.0, LEAN: 0.5, PASS: 0.0}
TAKE_MULT = 1.5
START_BANKROLL = 100.0
STARTED = date(2026, 10, 30)  # the day labels shipped: the bankroll starts here
SIGNALS = ("value", "snipe", "g1", "g2")
FRESH = timedelta(minutes=10)  # Pinnacle freshness, as in the consensus (alerts/consensus.py)


def edge_and_threshold(a) -> tuple[float | None, float | None]:
    """The pick's edge and the threshold its signal applied to it (same units)."""
    d = a.details or {}
    if a.signal == "value":
        from betdata.alerts.value import EDGE, EDGE_SOLO

        kind = "prop" if a.player_name else "game"
        solo = d.get("two_book_or_model_only") and not d.get("confirmed")
        return d.get("edge"), (EDGE_SOLO if solo else EDGE)[kind]
    if a.signal == "snipe":
        from betdata.alerts.snipe import EDGE

        return d.get("edge"), EDGE
    if a.signal == "g2":
        from betdata.alerts.rungs import EDGE

        edge = (None if a.fair_prob is None or a.book_prob is None
                else a.fair_prob - a.book_prob)  # fmt: skip
        return edge, EDGE
    if a.signal == "g1":  # points below the opening total
        from betdata.nba.g1 import DROP

        op = d.get("open")
        return (None if op is None or a.line is None else op - a.line), DROP
    return None, None


def _latest(p, a, book: str, now: datetime):
    """The book's latest stored quote of the pick's side (poller database), or None."""
    from betdata.alerts.grade import _poller_obs, latest_close, utc

    cutoff = now + timedelta(seconds=1)
    if a.tip is not None:
        cutoff = min(cutoff, utc(a.tip))
    return latest_close(_poller_obs(p, a, book, cutoff), a.side, a.market, book)


def _better_line(market: str, side: str, alerted: float, now_line: float) -> float:
    """> 0 when `now_line` is better for the bettor than the alerted line."""
    if market == "spreads":
        return now_line - alerted
    return alerted - now_line if side == "over" else now_line - alerted


def available(p, a, now: datetime) -> bool | None:
    """The alerted price (or better) is the book's latest quote; None when nothing is stored."""
    c = _latest(p, a, a.book, now)
    if c is None or c.price is None:
        return None
    if a.market != "h2h" and (c.line is None or a.line is None
                              or _better_line(a.market, a.side, a.line, c.line) < 0):
        return False
    from betdata.alerts.rules import decimal

    return decimal(c.price) >= decimal(a.price)


def pinnacle_sides(p, a, now: datetime) -> bool | None:
    """A fresh Pinnacle price (<= 10 minutes old) at the pick's line whose no-vig probability
    of the side beats the alerted price's break-even: Pinnacle makes the pick +EV. None when
    there is no fresh Pinnacle price at that line."""
    from betdata.alerts.grade import utc

    c = _latest(p, a, "pinnacle", now)
    if c is None or now - utc(c.at) > FRESH or c.line != a.line:
        return None
    nv = no_vig(c.price, c.other_price)
    if nv is None:
        return None
    return nv[0] > implied(a.price)


def label(p, a, now: datetime) -> dict | None:
    """The fixed label rules for a new pick (module docstring); None for other signals."""
    if a.signal not in SIGNALS:
        return None
    d = a.details or {}
    edge, thr = edge_and_threshold(a)
    if edge is None or thr is None:
        return None
    src = d.get("edge_source")
    cb = d.get("consensus_before") or {}
    avail = available(p, a, now)
    if avail is None:  # nothing stored: the alert was priced from this very poll
        avail = True
    pin = pinnacle_sides(p, a, now)
    if not avail:
        lab = PASS
    elif (edge >= TAKE_MULT * thr - 1e-9 and (src == "news" or pin is True)
          and not (src == "book disagreement" and cb.get("two_book"))):
        lab = TAKE
    else:
        lab = LEAN
    return {"label": lab, "stake": STAKE[lab], "edge": round(edge, 4), "threshold": thr,
            "edge_source": src, "pinnacle_sides": pin, "available": avail}


def status(p, a, now: datetime) -> str | None:
    """Live / Moved / Gone for an upcoming pick (display only); None when nothing is stored."""
    c = _latest(p, a, a.book, now)
    if c is None or c.price is None:
        return None
    if available(p, a, now):
        return "Live"
    if a.market != "h2h" and (c.line is None or a.line is None
                              or _better_line(a.market, a.side, a.line, c.line) < 0):
        return "Gone"
    fair = a.fair_prob
    if fair is None:
        return "Moved"
    return "Gone" if implied(c.price) >= fair else "Moved"
