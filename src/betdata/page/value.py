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


def value_html(sport: str, p: Connection | None, now: datetime, et, extra: str = "",
               top: str = "") -> str:  # fmt: skip
    """`top`: HTML right under the intro (the simulated bankroll); `extra`: HTML at the end
    of the section (the all-time record)."""
    head = ("<section class='value'><h2>Value</h2><p class='meta'>Paper alerts, not advice. "
            "A DK/FD price is alerted when it has not moved since the news and the repriced "
            "consensus (Pinnacle when fresh, else the median of fresh books) beats it by 3 pp "
            "(game markets) or 6 pp (props); more with a two-book or model-only price. Graded "
            f"by CLV against the consensus close; a signal is judged at {GATE_N} graded alerts "
            f"(an early read at {EARLY_N}).</p>{top}")  # fmt: skip
    if p is None:
        return (head + "<p class='meta'>Value alerts unavailable: no poller database.</p>"
                + extra + "</section>")
    from sqlalchemy import inspect

    if not {"alerts", "alert_grades", "consensus_grades"} <= set(inspect(p).get_table_names()):
        # The read-only copy predates M18's tables (the poller adds them on its next job):
        # there can be no M18 alert in it yet.
        return (head + "<h3>Live</h3><p class='meta'>No live value alerts for upcoming games."
                "</p><h3>Signals</h3><ul>" + "".join(
                    f"<li><b>{escape(n)}</b> {escape(SIGNAL_TEXT[n])}{gate_line(n, None)}</li>"
                    for n in _names(sport)) + "</ul>" + extra
                + "</section>")
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
        out.append("<ul class='plist'>" + "".join(_alert_li(r, et, False, p, now) for r in live)
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
            items.append(f"<li><b>{escape(name)}</b> {escape(SIGNAL_TEXT[name])}"
                         f"{gate_line(name, None)}</li>")
            continue
        items.append(f"<li><b>{escape(name)}</b> {escape(SIGNAL_TEXT[name])}"
                     f"{gate_line(name, s)}</li>")
    out.append("<h3>Signals</h3><ul>" + "".join(items) + "</ul>" + extra + "</section>")
    return "".join(out)


def gate_line(name: str, s: dict | None) -> str:
    """The small line under a signal's name: how proven it is ("C-NBA: 12/200 graded, CLV
    +0.4 pp"), then its alerts, record and gate status."""
    if s is None:
        return f"<div class='gate'>{escape(name)}: 0/{GATE_N} graded, no alerts yet</div>"
    rng = ("" if s["lo"] is None else
           f" (95% range {100 * s['lo']:+.1f} to {100 * s['hi']:+.1f})")
    roi = "–" if s["roi"] is None else f"{100 * s['roi']:+.1f}%"
    return (f"<div class='gate'>{escape(name)}: {s['n']}/{GATE_N} graded, CLV {_pp(s['clv'])}"
            f"{rng} · {s['alerts']} alerts · {s['wins']}-{s['losses']}, ROI {roi} · "
            f"{escape(s['status'])}</div>")  # fmt: skip


def label_badge(label: str | None, stake) -> str:
    """The pick's label and stake as a badge (alerts/labels.py); empty for unlabelled picks."""
    if not label:
        return ""
    st = float(stake) if stake not in (None, "") else 0.0
    return (f" <span class='lbl {escape(label.lower())}'>{escape(label)} "
            f"{st:g}u</span>")


def _names(sport: str) -> list[str]:
    return {"nba": ["C-NBA", "F", "G1"], "nfl": ["C-NFL", "G2"]}.get(sport, [])


def _utc(t: datetime) -> datetime:
    from datetime import UTC

    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _alert_li(r, et, graded: bool, p: Connection | None = None,
              now: datetime | None = None) -> str:  # fmt: skip
    a = type("A", (), dict(r))  # selection() reads attributes
    sel = selection(a)
    book = BOOKS.get(r["book"], r["book"])
    name = signal_name(r["signal"], r["sport"])
    fair = "" if r["fair_prob"] is None else f" · fair {100 * r['fair_prob']:.0f}%"
    nv = "" if r["book_prob"] is None else f" vs no-vig {100 * r['book_prob']:.0f}%"
    d = r["details"] or {}
    if isinstance(d, str):
        import json

        d = json.loads(d)
    lab = d.get("label") or {}
    badge = label_badge(lab.get("label"), lab.get("stake"))
    if not graded and badge and p is not None and now is not None:
        from betdata.alerts.labels import status

        try:
            st = status(p, a, now)
        except Exception:  # noqa: BLE001 - a status never drops the pick
            st = None
        if st:
            badge += f" <span class='st {st.lower()}'>{st}</span>"
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
            f"{_price(r['price'])}</b> at {escape(book)}{badge} <span class='research'>"
            f"{escape(name)}</span></div><div class='meta'>"
            f"{escape(et(_utc(r['created_at']), '%a %-I:%M %p'))}{fair}{nv}{tail}</div>"
            f"<div class='meta'>{escape(r['reason'][:300])}</div></li>")


def _cell_clv(r, pin: bool = False) -> str:
    n, mu, lo, hi = r.clv_stats(pin)
    if mu is None:
        return "–"
    rng = "" if lo is None else f" ({100 * lo:+.1f} to {100 * hi:+.1f})"
    return f"{100 * mu:+.2f} pp{rng}, n={n}"


def record_html(rows: list[dict], csv_href: str = "picks.csv") -> str:
    """The all-time record of every pick in the permanent ledger (betdata/ledger.py): overall,
    by sport, by signal, props vs game markets, and the C signals by edge source."""
    from betdata.ledger import LEGACY, TOO_FEW, breakdowns, counted

    n_legacy = sum(r["signal_name"] == LEGACY for r in rows)
    excl = ("" if not n_legacy else f"<p class='meta'>Excludes {n_legacy} M16 legacy alerts "
            "(pre-M18 rules), available in the CSV.</p>")
    rows = counted(rows)
    if not rows:
        return ("<h3>All-time record</h3><p class='meta'>No picks in the ledger yet. "
                f"<a href='{escape(csv_href)}'>Download all picks (CSV)</a></p>" + excl)
    b = breakdowns(rows)
    body = []
    for group, recs in b.items():
        if not recs:
            continue
        if group != "all":
            body.append(f"<tr class='grp'><th colspan='7'>{escape(group.capitalize())}</th></tr>")
        for k, r in recs.items():
            roi = "–" if r.roi is None else f"{100 * r.roi:+.1f}%"
            judge = " <small>too few to judge</small>" if r.graded < TOO_FEW else ""
            body.append(f"<tr><td>{escape(k)}{judge}</td><td>{r.picks}</td>"
                        f"<td>{r.wins}-{r.losses}-{r.pushes}</td><td>{r.units:+.2f}</td>"
                        f"<td>{roi}</td><td>{_cell_clv(r)}</td><td>{_cell_clv(r, True)}</td>"
                        "</tr>")  # fmt: skip
    return ("<h3>All-time record</h3><p class='meta'>Every alert is a pick: 1 unit at the "
            "alerted price, voids kept but left out of W-L and ROI. CLV against the consensus "
            "close, and separately against Pinnacle's close where one exists. C signals are "
            "split by edge source: \"news\" when the repricer's move supplies at least half "
            f"the edge, else \"book disagreement\". Under {TOO_FEW} graded picks a record is "
            "too few to judge. <a href='" + escape(csv_href) + "'>Download all picks (CSV)</a>"
            "</p><div class='tablewrap'><table class='lines record'><tr><th></th><th>Picks</th>"
            "<th>W-L-P</th><th>Units</th><th>ROI</th><th>CLV vs consensus</th>"
            "<th>CLV vs Pinnacle</th></tr>" + "".join(body) + "</table></div>" + excl)


def bank_html(rows: list[dict], title: str) -> str:
    """The simulated bankroll of the labelled picks among `rows` (betdata/ledger.py `bank`):
    current bankroll, units won or lost and ROI, then the record by label."""
    from betdata.alerts.labels import LEAN, START_BANKROLL, STARTED, TAKE
    from betdata.ledger import bank

    b = bank(rows)
    roi = "–" if b.roi is None else f"{100 * b.roi:+.1f}%"
    body = []
    for lab in (TAKE, LEAN):
        x = bank([r for r in rows if r.get("label") == lab])
        r_roi = "–" if x.roi is None else f"{100 * x.roi:+.1f}%"
        clv = "–" if not x.clv else f"{100 * sum(x.clv) / len(x.clv):+.1f}"
        body.append(f"<tr><td>{label_badge(lab, 1.0 if lab == TAKE else 0.5)}</td>"
                    f"<td>{x.picks}</td><td>{x.wins}-{x.losses}-{x.pushes}</td>"
                    f"<td>{x.units:+.2f}u</td><td>{r_roi}</td><td>{clv}</td></tr>")
    return (f"<div class='bank'><h3>Simulated bankroll · {escape(title)}</h3>"
            f"<p class='bankline'><b>{b.bankroll:.2f}u</b> · {b.units:+.2f}u · ROI {roi} · "
            f"{b.picks} labelled picks ({b.wins}-{b.losses}-{b.pushes})</p>"
            f"<p class='meta'>In-house simulation: started at {START_BANKROLL:g} units on "
            f"{STARTED.strftime('%b %-d, %Y')}; flat stakes (TAKE 1 unit, LEAN 0.5), no "
            "compounding; paper units, not advice. Earlier picks are unlabelled and not "
            "counted.</p><div class='tablewrap'><table class='lines record'><tr><th>Label</th>"
            "<th>Picks</th><th>W-L-P</th><th>Units</th><th>ROI</th><th>CLV pp</th></tr>"
            + "".join(body) + "</table></div></div>")
