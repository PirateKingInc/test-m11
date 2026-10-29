"""M18 Part H: grading against the consensus close, and the per-signal gates (MODEL.md
"M18 Part H").

- **Consensus close:** the Part B consensus at the last poll before tip (Pinnacle if fresh,
  else the median of fresh books).
- **CLV:** the close's probability of the alerted side at the alerted line, minus the alerted
  book's no-vig probability of that side at alert time. Game markets carry the close to the
  alerted line on the fair scale; a prop close at another line is carried with the prop sigma.
- **Gate (per signal):** after 200 graded alerts the signal is "working" only if its mean CLV
  is positive and the 95% range (mean +- 1.96 sd/sqrt(n)) excludes zero. At 60, an "early
  read" with the same statistics.
- The result and ROI at the alerted price stay in `alert_grades` (M16 grading).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Connection, Engine, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from betdata.alerts import consensus as C
from betdata.alerts.db import alert_grades, alerts, consensus_grades, poll_marks, quotes
from betdata.alerts.grade import mean_range, utc
from betdata.db.schema import games

GATE_N, EARLY_N = 200, 60
# signal (and sport) -> the M18 signal name
NAMES = {("value", "nba"): "C-NBA", ("value", "nfl"): "C-NFL", ("snipe", "nba"): "F",
         ("g1", "nba"): "G1", ("g2", "nfl"): "G2"}  # fmt: skip


def signal_name(signal: str, sport: str) -> str:
    return NAMES.get((signal, sport), f"{signal}-{sport}")


def last_poll_before(p: Connection, game_id: int, tip: datetime) -> datetime | None:
    t1 = p.execute(select(poll_marks.c.polled_at).where(
        poll_marks.c.game_id == game_id, poll_marks.c.polled_at < tip)
        .order_by(poll_marks.c.polled_at.desc()).limit(1)).scalar()  # fmt: skip
    t2 = p.execute(select(quotes.c.polled_at).where(
        quotes.c.game_id == game_id, quotes.c.polled_at < tip)
        .order_by(quotes.c.polled_at.desc()).limit(1)).scalar()  # fmt: skip
    ts = [utc(t) for t in (t1, t2) if t is not None]
    return max(ts) if ts else None


def close_prob(c: C.Consensus, a) -> float | None:
    """The consensus's probability of the alert's side at the alert's line."""
    if a.player_name and c.line is not None and a.line is not None and c.line != a.line:
        from betdata.alerts.value import carried

        return carried(a.sport, a.market, c.line, c.fair, a.side, a.line)
    return c.prob(a.side, a.line)


def consensus_close(m: Connection, p: Connection, a, tip: datetime) -> C.Consensus | None:
    t = last_poll_before(p, a.game_id, tip)
    if t is None:
        return None
    if a.player_name:
        return (C.prop_consensus(p, a.sport, a.game_id, a.market, a.player_name, t, a.line)
                or C.prop_consensus(p, a.sport, a.game_id, a.market, a.player_name, t))
    return C.game_consensus(p, m, a.sport, a.game_id, a.market, t)


def grade_consensus(main: Engine, pdb: Engine, now: datetime) -> int:
    """Grade every alert whose game has started against its consensus close (once a close is
    found it is final); returns grades written."""
    written = 0
    with pdb.begin() as p, main.connect() as m:
        have = {r.alert_id: r for r in p.execute(select(consensus_grades))}
        for a in p.execute(select(alerts).where(alerts.c.tip < now)).all():
            old = have.get(a.alert_id)
            if old is not None and old.close_prob is not None:
                continue
            g = m.execute(select(games.c.start_time_utc).where(
                games.c.game_id == a.game_id)).first()  # fmt: skip
            tip = utc(g.start_time_utc) if g else utc(a.tip)
            if tip >= now:
                continue
            c = consensus_close(m, p, a, tip)
            pr = None if c is None else close_prob(c, a)
            vals = dict(close_at=None if c is None else c.at,
                        close_method=None if c is None else c.method,
                        close_n=None if c is None else c.n,
                        close_two_book=None if c is None else c.two_book,
                        close_fair=None if c is None else c.fair,
                        close_line=None if c is None else c.line, close_prob=pr,
                        clv=None if pr is None or a.book_prob is None
                        else round(pr - a.book_prob, 4), graded_at=now)  # fmt: skip
            if old is None:
                p.execute(sqlite_insert(consensus_grades).values(alert_id=a.alert_id, **vals))
            else:
                p.execute(update(consensus_grades)
                          .where(consensus_grades.c.alert_id == a.alert_id).values(**vals))
            written += 1
    return written


def gate(clvs: list[float]) -> dict:
    mu, lo, hi = mean_range(clvs)
    n = len(clvs)
    if n >= GATE_N:
        status = "working" if mu > 0 and lo > 0 else "not working"
    elif n >= EARLY_N:
        status = ("early read: " + ("positive, range excludes 0" if mu > 0 and lo > 0 else
                                    "range includes 0" if lo is not None and lo <= 0 <= hi
                                    else "negative"))  # fmt: skip
    else:
        status = f"no read before {EARLY_N} graded ({n} so far); verdict at {GATE_N}"
    return {"n": n, "clv": mu, "lo": lo, "hi": hi, "status": status}


def signal_rows(p: Connection) -> list:
    return p.execute(select(alerts, consensus_grades.c.clv, consensus_grades.c.close_prob,
                            consensus_grades.c.close_method, consensus_grades.c.close_n,
                            consensus_grades.c.close_two_book, alert_grades.c.result,
                            alert_grades.c.profit)
                     .outerjoin(consensus_grades,
                                consensus_grades.c.alert_id == alerts.c.alert_id)
                     .outerjoin(alert_grades, alert_grades.c.alert_id == alerts.c.alert_id)
                     ).mappings().all()  # fmt: skip


def gates(p: Connection, signals=("value", "snipe", "g1", "g2")) -> dict[str, dict]:
    """Per M18 signal: alerts, graded, the CLV gate, and W-L / ROI at the alerted price."""
    out: dict[str, dict] = {}
    rows = [r for r in signal_rows(p) if r["signal"] in signals]
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(signal_name(r["signal"], r["sport"]), []).append(r)
    for name, rs in sorted(by.items()):
        clvs = [r["clv"] for r in rs if r["clv"] is not None]
        settled = [r for r in rs if r["result"] in ("win", "loss", "push")]
        out[name] = {"alerts": len(rs), **gate(clvs),
                     "wins": sum(r["result"] == "win" for r in settled),
                     "losses": sum(r["result"] == "loss" for r in settled),
                     "roi": (sum(r["profit"] for r in settled) / len(settled)
                             if settled else None)}  # fmt: skip
    return out
