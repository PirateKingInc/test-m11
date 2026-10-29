"""The "Value" section at the top of each sport's tab (M18 Page, MODEL.md "M18 Page").

It is read from the poller database (read only; the hourly job restores it before the run):
- the sport's live M18 alerts, for games not yet started: stale DK/FD prices after news (C),
  opening-line prop snipes (F), early-season totals (G1) and NFL rungs (G2);
- the most recent graded ones, with the consensus close and CLV;
- per signal: the running CLV against the consensus close, the count toward 200 and the ROI at
  the alerted price (alerts/gates.py).

Without a poller database the section says so; any failure drops it, never the page.
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from sqlalchemy import Connection, select

from betdata.alerts.db import alert_grades, alerts, consensus_grades
from betdata.alerts.gates import EARLY_N, GATE_N, gates, signal_name
from betdata.alerts.notify import BOOKS, MARKETS, selection

M18_SIGNALS = ("value", "snipe", "g1", "g2")
SIGNAL_TEXT = {
    "C-NBA": "Stale DK/FD price after NBA news (repriced from the consensus)",
    "C-NFL": "Stale DK/FD price after NFL news (lag study)",
    "F": "NBA opening-line prop snipes (rebounds, assists)",
    "G1": "Early-season NBA totals (paper)",
    "G2": "NFL alternate and first-half rungs",
}
RECENT = 10


def _price(p: int) -> str:
    return f"+{p}" if p > 0 else str(p)


def _pp(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:+.1f} pp"


def value_html(sport: str, p: Connection | None, now: datetime, et) -> str:
    head = ("<section class='value'><h2>Value</h2><p class='meta'>Paper alerts, not advice. "
            "A DK/FD price is alerted when it has not moved since the news and the repriced "
            "consensus (Pinnacle when fresh, else the median of fresh books) beats it by 3 pp "
            "(game markets) or 6 pp (props); more with a two-book or model-only price. Graded "
            f"by CLV against the consensus close; a signal is judged at {GATE_N} graded alerts "
            f"(an early read at {EARLY_N}).</p>")  # fmt: skip
    if p is None:
        return head + "<p class='meta'>Value alerts unavailable: no poller database.</p></section>"
    from sqlalchemy import inspect

    if not {"alerts", "alert_grades", "consensus_grades"} <= set(inspect(p).get_table_names()):
        # The read-only copy predates M18's tables (the poller adds them on its next job):
        # there can be no M18 alert in it yet.
        return (head + "<h3>Live</h3><p class='meta'>No live value alerts for upcoming games."
                "</p><h3>Signals</h3><ul>" + "".join(
                    f"<li><b>{escape(n)}</b> {escape(SIGNAL_TEXT[n])}: no alerts yet "
                    f"(0 of {GATE_N}).</li>" for n in _names(sport)) + "</ul></section>")
    rows = p.execute(
        select(alerts, consensus_grades.c.clv, consensus_grades.c.close_prob,
               consensus_grades.c.close_method, consensus_grades.c.close_two_book,
               alert_grades.c.result)
        .outerjoin(consensus_grades, consensus_grades.c.alert_id == alerts.c.alert_id)
        .outerjoin(alert_grades, alert_grades.c.alert_id == alerts.c.alert_id)
        .where(alerts.c.sport == sport, alerts.c.signal.in_(M18_SIGNALS))
        .order_by(alerts.c.created_at.desc())).mappings().all()  # fmt: skip
    live = [r for r in rows if r["tip"] is not None and _utc(r["tip"]) > now]
    done = [r for r in rows if r not in live][:RECENT]
    out = [head, "<h3>Live</h3>"]
    if live:
        out.append("<ul class='plist'>" + "".join(_alert_li(r, et, False) for r in live)
                   + "</ul>")
    else:
        out.append("<p class='meta'>No live value alerts for upcoming games.</p>")
    if done:
        out.append("<h3>Recent, graded against the consensus close</h3><ul class='plist'>"
                   + "".join(_alert_li(r, et, True) for r in done) + "</ul>")
    g = {k: v for k, v in gates(p).items() if k in _names(sport)}
    items = []
    for name in _names(sport):
        s = g.get(name)
        if s is None:
            items.append(f"<li><b>{escape(name)}</b> {escape(SIGNAL_TEXT[name])}: no alerts "
                         f"yet (0 of {GATE_N}).</li>")
            continue
        rng = ("" if s["lo"] is None else
               f" (95% range {100 * s['lo']:+.1f} to {100 * s['hi']:+.1f})")
        roi = "–" if s["roi"] is None else f"{100 * s['roi']:+.1f}%"
        items.append(f"<li><b>{escape(name)}</b> {escape(SIGNAL_TEXT[name])}: "
                     f"{s['alerts']} alerts, {s['n']} of {GATE_N} graded; CLV vs consensus "
                     f"close {_pp(s['clv'])}{rng}; {s['wins']}-{s['losses']}, ROI {roi}. "
                     f"{escape(s['status'])}.</li>")  # fmt: skip
    out.append("<h3>Signals</h3><ul>" + "".join(items) + "</ul></section>")
    return "".join(out)


def _names(sport: str) -> list[str]:
    return {"nba": ["C-NBA", "F", "G1"], "nfl": ["C-NFL", "G2"]}.get(sport, [])


def _utc(t: datetime) -> datetime:
    from datetime import UTC

    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _alert_li(r, et, graded: bool) -> str:
    a = type("A", (), dict(r))  # selection() reads attributes
    sel = selection(a)
    book = BOOKS.get(r["book"], r["book"])
    name = signal_name(r["signal"], r["sport"])
    fair = "" if r["fair_prob"] is None else f" · fair {100 * r['fair_prob']:.0f}%"
    nv = "" if r["book_prob"] is None else f" vs no-vig {100 * r['book_prob']:.0f}%"
    tail = ""
    if graded:
        if r["clv"] is not None:
            tail = (f" · close {100 * r['close_prob']:.0f}% ({escape(r['close_method'] or '')}"
                    f"{' two-book' if r['close_two_book'] else ''}) · CLV {_pp(r['clv'])}")
        else:
            tail = " · no consensus close"
        if r["result"]:
            tail += f" · {escape(r['result'])}"
    return (f"<li class='prop'><div><b>{escape(r['game_label'] or '')}</b> · "
            f"{escape(MARKETS.get(r['market'], r['market']))}: <b>{escape(sel)} "
            f"{_price(r['price'])}</b> at {escape(book)} <span class='research'>{escape(name)}"
            f"</span></div><div class='meta'>{escape(et(_utc(r['created_at']), '%a %-I:%M %p'))}"
            f"{fair}{nv}{tail}</div><div class='meta'>{escape(r['reason'][:300])}</div></li>")
