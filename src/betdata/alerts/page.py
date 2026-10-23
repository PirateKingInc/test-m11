"""The page's "Alerts" section (issue #168): an isolated post-step on the built site.

`betdata alerts page` reads the poller database read-only and inserts one fragment into the
already-built `site/index.html` (before `</main>`, between markers, so a second run replaces
it) and writes `site/alerts.json`. The hourly page is generated exactly as before; if the
poller database is missing or unreadable, the section says "Alerts unavailable", and any other
failure leaves the page untouched.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select

from betdata.alerts.db import alert_grades, alerts, get_state, status_events
from betdata.alerts.grade import VERDICT_N, group_stats, verdict
from betdata.alerts.notify import BOOKS, MARKETS, selection
from betdata.alerts.rules import EASTERN

log = logging.getLogger(__name__)
START, END = "<!-- alerts:start -->", "<!-- alerts:end -->"
RECENT = 30
SIGNALS = {"news": "News (Signal 1)", "sharp": "Soft vs sharp (Signal 2)"}
LABEL = "Paper alerts, not advice."
CAVEAT = (f"No signal is judged before {VERDICT_N} graded alerts; until then the running CLV is "
          "noise as much as signal.")  # fmt: skip


def _ro_engine(db: Path):
    if not db.is_file():
        raise FileNotFoundError(db)
    return create_engine(f"sqlite:///file:{db.resolve()}?mode=ro&uri=true", future=True)


def lag_study(c, now: datetime) -> dict:
    """Qualifying changes so far, the DK+FD pooled median lag, and Signal 1's status."""
    from betdata.alerts import lag as L

    n = c.execute(select(func.count()).select_from(status_events)
                  .where(status_events.c.qualifies.is_(True))).scalar()  # fmt: skip
    s = L.summary(L.all_lags(c, None, now))
    decision = get_state(c, "signal1:decision")
    return {"changes": n, "soft_n": s["soft_n"], "soft_median": s["soft_median"],
            "signal1": decision["signal1"] if decision else "on (study running)"}  # fmt: skip


def load(db: Path, now: datetime | None = None) -> dict:
    """Recent alerts (newest first) with their grades, the running CLV per signal, and the
    lag study's status."""
    now = now or datetime.now(UTC)
    eng = _ro_engine(db)
    try:
        with eng.connect() as c:
            try:
                lag = lag_study(c, now)
            except Exception as e:  # noqa: BLE001 - the lag line is optional
                log.warning("lag study line skipped: %s", e)
                lag = None
            rows = c.execute(select(alerts, alert_grades.c.clv_points, alert_grades.c.clv_novig,
                                    alert_grades.c.result, alert_grades.c.profit)
                             .outerjoin(alert_grades,
                                        alert_grades.c.alert_id == alerts.c.alert_id)
                             .order_by(alerts.c.created_at.desc(), alerts.c.alert_id.desc())
                             ).mappings().all()  # fmt: skip
    finally:
        eng.dispose()
    graded = [r for r in rows if r["clv_novig"] is not None or r["result"] is not None]
    signals = {}
    for sig in SIGNALS:
        s = group_stats([r for r in graded if r["signal"] == sig])
        signals[sig] = {**s, "verdict": verdict(s)}
    recent = [{
        "alert_id": r["alert_id"], "created_at": r["created_at"].isoformat(),
        "signal": r["signal"], "sport": r["sport"], "game": r["game_label"],
        "market": r["market"], "selection": _selection(r), "book": r["book"],
        "price": r["price"], "line": r["line"], "reason": r["reason"],
        "delivery": "sent" if r["sent"] else "quiet" if r["quiet"] else "not sent",
        "study": bool(r["study"]), "result": r["result"], "profit": r["profit"],
        "clv_points": r["clv_points"], "clv_novig": r["clv_novig"],
    } for r in rows[:RECENT]]  # fmt: skip
    return {"available": True, "alerts": recent, "signals": signals, "total": len(rows), "lag": lag}


def _selection(r) -> str:
    try:
        return selection(SimpleNamespace(**{k: r[k] for k in (
            "game_label", "side", "player_name", "line", "market")}))  # fmt: skip
    except Exception:  # noqa: BLE001 - an odd label never drops the row
        return f"{r['market']} {r['side']} {'' if r['line'] is None else r['line']}".strip()


def _et(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(EASTERN).strftime("%a %b %-d %-I:%M %p")


def _pp(x, sign=True) -> str:
    return "" if x is None else f"{100 * x:{'+' if sign else ''}.1f}"


def fragment(data: dict | None) -> str:
    head = f'{START}<section id="alerts"><h2>Alerts</h2><p><b>{escape(LABEL)}</b></p>'
    if not data or not data.get("available"):
        return f"{head}<p class='meta'>Alerts unavailable.</p></section>{END}"
    parts = [head, f"<p class='meta'>{escape(CAVEAT)} CLV no-vig in % points of the bet side's "
             "probability vs the pre-game close.</p>", "<h3>Running CLV</h3><table><tr>"
             "<th>Signal</th><th>Graded</th><th>Avg CLV no-vig (95% range)</th><th>ROI</th>"
             "<th>Verdict</th></tr>"]  # fmt: skip
    for sig, s in data["signals"].items():
        rng = "" if s["lo"] is None else f" [{_pp(s['lo'])}, {_pp(s['hi'])}]"
        avg = "-" if s["clv_novig"] is None else f"{_pp(s['clv_novig'])}{rng}"
        roi = "-" if s["roi"] is None else f"{100 * s['roi']:+.1f}%"
        parts.append(f"<tr><td>{escape(SIGNALS[sig])}</td><td>{s['n_novig']}</td>"
                     f"<td>{escape(avg)}</td><td>{roi}</td><td>{escape(s['verdict'])}</td></tr>")
    parts.append("</table>")
    if data.get("lag"):
        lg = data["lag"]
        med = ("not enough data" if lg["soft_median"] is None else
               f"{lg['soft_median']:.1f} min over {lg['soft_n']} lags")  # fmt: skip
        parts.append(f"<p class='meta'>Lag study: {lg['changes']} qualifying changes so far; "
                     f"DK+FD pooled median lag {escape(med)}; Signal 1 "
                     f"{escape(lg['signal1'])}.</p>")  # fmt: skip
    if not data["alerts"]:
        parts.append("<p class='meta'>No alerts yet.</p>")
    else:
        parts.append("<h3>Recent alerts</h3><div style='overflow-x:auto'><table><tr>"
                     "<th>Time (ET)</th><th>Signal</th><th>Game</th><th>Bet</th><th>Book</th>"
                     "<th>Price</th><th>Why</th><th>Push</th><th>Grade</th></tr>")  # fmt: skip
        for a in data["alerts"]:
            reason = a["reason"] if len(a["reason"]) <= 90 else a["reason"][:89] + "…"
            grade = "" if a["result"] is None else a["result"]
            if a["clv_novig"] is not None:
                grade += f" CLV {_pp(a['clv_novig'])} pp"
            if a["clv_points"] is not None and a["market"] != "h2h":
                grade += f" ({a['clv_points']:+g} pts)"
            price = f"+{a['price']}" if a["price"] > 0 else str(a["price"])
            study = " (study)" if a["study"] else ""
            parts.append(
                f"<tr><td>{escape(_et(a['created_at']))}</td>"
                f"<td>{escape(a['signal'])}{study}</td>"
                f"<td>{escape(a['sport'].upper())} {escape(a['game'] or '')}</td>"
                f"<td>{escape(MARKETS.get(a['market'], a['market']))}: "
                f"{escape(a['selection'])}</td><td>{escape(BOOKS.get(a['book'], a['book']))}</td>"
                f"<td>{price}</td><td>{escape(reason)}</td><td>{a['delivery']}</td>"
                f"<td>{escape(grade.strip()) or 'pending'}</td></tr>")  # fmt: skip
        parts.append("</table></div>")
    return "".join(parts) + "</section>" + END


def insert(html: str, frag: str) -> str:
    """The page with the fragment before `</main>` (else `</body>`); an earlier fragment is
    replaced. A page with neither tag is returned unchanged."""
    if START in html and END in html:
        head, rest = html.split(START, 1)
        html = head + rest.split(END, 1)[1]
    for tag in ("</main>", "</body>"):
        i = html.rfind(tag)
        if i >= 0:
            return html[:i] + frag + html[i:]
    return html


def publish(db: Path, site: Path, now: datetime | None = None) -> str:
    """Insert the section into `site/index.html` and write `site/alerts.json`. Never raises."""
    now = now or datetime.now(UTC)
    index = site / "index.html"
    try:
        if not index.is_file():
            return "no site/index.html: nothing to do"
        try:
            data = load(db, now)
        except Exception as e:  # noqa: BLE001 - a missing or broken poller DB
            log.warning("alerts unavailable: %s", e)
            data = {"available": False, "error": f"{type(e).__name__}"}
        html = insert(index.read_text(), fragment(data))
        (site / "alerts.json").write_text(json.dumps(
            {"generated_at": now.isoformat(), **data}, indent=1, default=str))  # fmt: skip
        index.write_text(html)
        return "alerts section " + ("added" if data["available"] else "unavailable")
    except Exception as e:  # noqa: BLE001 - never break the page
        log.warning("alerts page step failed: %s", e)
        return f"alerts page step failed: {type(e).__name__}"
