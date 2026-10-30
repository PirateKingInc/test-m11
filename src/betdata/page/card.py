"""The "Tonight's card" section at the top of each sport tab, above the Value section (owner
request 2026-10-30; alerts/card.py chooses the card, betdata/ledger.py keeps it).

It reads the ledger (card yes/no, the card's label and stake, results) and, for the display
status of upcoming picks, the poller database (read only). It shows the sport's latest card:
each pick with its game, start (ET), selection, line, odds, book, label badge and stake, and a
status (Live / Moved / Gone, then the result once graded); a line per contributing signal with
its gate status; and the card bankroll (card picks only): this sport's, plus "All sports" once
on the NBA tab. Any failure drops the section, never the page.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from types import SimpleNamespace

from betdata.alerts.labels import LEAN, START_BANKROLL, TAKE
from betdata.alerts.rules import EASTERN
from betdata.page.value import _price, gate_line, label_badge


def _a(r: dict) -> SimpleNamespace:
    """A ledger row as the alert fields the selection text and the status read."""
    return SimpleNamespace(
        game_id=int(r["game_id"]), game_label=r["game"] or "? @ ?", market=r["market"],
        player_name=r["player"] or None, side=r["side"],
        line=float(r["line"]) if r["line"] else None, price=int(r["odds"]), book=r["book"],
        fair_prob=float(r["fair_prob"]) if r["fair_prob"] else None,
        tip=datetime.fromisoformat(r["tip"]) if r["tip"] else None)  # fmt: skip


def _row(r: dict, p, now: datetime, et) -> str:
    from betdata.alerts.notify import BOOKS, selection

    a = _a(r)
    stake = float(r["card_stake"] or 0)
    if r["result"] in ("win", "loss", "push", "void"):
        units = 0.0 if r["result"] == "void" else stake * float(r["profit"] or 0)
        cls = {"win": "live", "loss": "gone"}.get(r["result"], "moved")
        st = f"<span class='st {cls}'>{escape(r['result'].title())} {units:+.2f}u</span>"
    else:
        from betdata.alerts.labels import status

        try:
            s = status(p, a, now) if p is not None else None
        except Exception:  # noqa: BLE001 - a status never drops the pick
            s = None
        st = "" if not s else f"<span class='st {s.lower()}'>{s}</span>"
    when = "" if a.tip is None else et(a.tip, "%a %-I:%M %p")
    return (f"<li class='prop'><div><b>{escape(a.game_label)}</b> · {escape(when)}</div>"
            f"<div><b>{escape(selection(a))} {_price(a.price)}</b> at "
            f"{escape(BOOKS.get(a.book, a.book))}"
            f"{label_badge(r['card_label'], r['card_stake'])} {st}</div></li>")


def card_bank_html(rows: list[dict], title: str) -> str:
    """The card bankroll (card picks only, flat stakes from 100 units) and its record by
    label."""
    from betdata.ledger import bank

    rows = [r for r in rows if r.get("card") == "yes"]
    b = bank(rows, "card_label", "card_stake")
    roi = "–" if b.roi is None else f"{100 * b.roi:+.1f}%"
    body = []
    for lab in (TAKE, LEAN):
        x = bank([r for r in rows if r.get("card_label") == lab], "card_label", "card_stake")
        r_roi = "–" if x.roi is None else f"{100 * x.roi:+.1f}%"
        body.append(f"<tr><td>{label_badge(lab, 1.0 if lab == TAKE else 0.5)}</td>"
                    f"<td>{x.picks}</td><td>{x.wins}-{x.losses}-{x.pushes}</td>"
                    f"<td>{x.units:+.2f}</td><td>{r_roi}</td></tr>")
    return (f"<div class='bank'><h3>Card bankroll · {escape(title)}</h3>"
            f"<p class='bankline'><b>{b.bankroll:.2f}u</b> · {b.units:+.2f}u · ROI {roi} · "
            f"{b.wins}-{b.losses}-{b.pushes} ({b.picks} card picks)</p>"
            f"<p class='meta'>Card picks only, from {START_BANKROLL:g} units; flat stakes "
            "(TAKE 1 unit, LEAN 0.5), no compounding.</p><div class='tablewrap'>"
            "<table class='lines record'><tr><th>Label</th><th>Picks</th><th>W-L-P</th>"
            "<th>Units</th><th>ROI</th></tr>" + "".join(body) + "</table></div></div>")


def card_html(sport: str, name: str, rows: list[dict] | None, p, now: datetime, et,
              next_at: datetime | None = None, all_rows: list[dict] | None = None) -> str:
    """The section (module docstring). `rows`: every ledger row (betdata/ledger.py `joined`);
    `next_at`: when today's card is chosen, if it is not chosen yet; `all_rows`: given on the
    tab that shows the combined "All sports" card record."""
    from betdata.alerts.gates import gates
    from betdata.page.value import _names

    head = ("<section class='cardsec'><h2>Tonight's card</h2><p class='meta'>In-house simulation "
            "of a pick service: up to 5 Value picks still live, by expected value, chosen once "
            "at 8:50 AM ET (or at the day's first live pick, if none is live then). Paper "
            "units, not advice.</p>")  # fmt: skip
    own = [r for r in rows or [] if r["sport"] == sport]
    days = sorted({r["card_date"] for r in own if r.get("card_date")})
    out = [head]
    today = now.astimezone(EASTERN).date().isoformat()
    if next_at is not None and (not days or days[-1] != today):
        out.append(f"<p class='meta'>Today's card is chosen at "
                   f"{escape(et(next_at, '%-I:%M %p'))}.</p>")
    names = _names(sport)
    if days:
        day = days[-1]
        picks = [r for r in own if r["card_date"] == day and r["card"] == "yes"]
        picks.sort(key=lambda r: (r["tip"], r["game"]))
        label_day = datetime.fromisoformat(day).strftime("%a %b %-d")
        out.append(f"<h3>{escape(label_day)}</h3>")
        if picks:
            out.append("<ul class='plist'>" + "".join(_row(r, p, now, et) for r in picks)
                       + "</ul>")
            on = {r["signal_name"] for r in picks}
            names = [n for n in names if n in on] + sorted(on - set(names))
        else:
            out.append("<p class='meta'>No live picks qualified for this card.</p>")
    elif next_at is None:
        from betdata.alerts.card import FIRST_DAY

        out.append("<p class='meta'>No card yet: the first is chosen at 8:50 AM ET on "
                   f"{FIRST_DAY.strftime('%a %b %-d')}.</p>")
    g = {}
    if p is not None:
        try:
            g = gates(p)
        except Exception:  # noqa: BLE001 - the gate lines never drop the card
            g = {}
    out.append("<div class='gates'>" + "".join(gate_line(n, g.get(n)) for n in names)
               + "</div>")
    if all_rows is not None:
        out.append(card_bank_html(all_rows, "All sports"))
    out.append(card_bank_html(own, name))
    return "".join(out) + "</section>"
