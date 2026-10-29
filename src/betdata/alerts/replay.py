"""M18 verification: replay the value rule on the stored NBA news events (MODEL.md "M18 Part C").

The M16 poller kept every qualifying NBA status change (`status_events`), the frozen models'
re-prediction (`repredictions`) and every price it polled (`quotes`). On a throwaway copy of the
poller database, each event is run through `Poller.signal_value`:
- **As of:** the moment it was detected, with the quotes of the first poll after detection.
- **Not pushed:** nothing is sent.

The replayed alerts are then graded against the consensus close. The M16 Signal 1 alerts of the
same events are listed beside them.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import Engine, func, select

from betdata.alerts.db import alerts, engine_for, quotes, repredictions, status_events
from betdata.alerts.feeds import Quote
from betdata.alerts.grade import utc


def replay(main: Engine, poller_db: Path, work: Path, now: datetime) -> dict:
    from betdata.alerts.gates import gates, grade_consensus
    from betdata.alerts.poller import Keys, Poller
    from betdata.nba.props.live import PropDesign, load_model, p_over_no_push

    work.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(poller_db, work)
    pdb = engine_for(work)
    mod, _ = load_model(PropDesign.load())

    def p_over(market, mean, line):
        return float(p_over_no_push(mod, market, [mean], [line])[0])

    pol = Poller(main, pdb, Keys(None, None, None), now_fn=lambda: now, lock=lambda: False)
    with pdb.connect() as p:
        before = p.execute(select(func.max(alerts.c.alert_id))).scalar() or 0
        evs = p.execute(select(status_events, repredictions.c.margin_before,
                               repredictions.c.margin_after, repredictions.c.total_before,
                               repredictions.c.total_after, repredictions.c.props)
                        .join(repredictions, repredictions.c.event_id == status_events.c.event_id)
                        .where(status_events.c.sport == "nba",
                               status_events.c.qualifies.is_(True))
                        .order_by(status_events.c.detected_at)).mappings().all()  # fmt: skip
        m16 = p.execute(select(alerts.c.event_id, alerts.c.market, alerts.c.side,
                               alerts.c.line, alerts.c.book)
                        .where(alerts.c.signal == "news", alerts.c.sport == "nba")).all()
    out = {"events": len(evs), "replayed": [], "m16_news_alerts": len(m16)}
    for ev in evs:
        with pdb.connect() as p:
            first = p.execute(select(func.min(quotes.c.polled_at)).where(
                quotes.c.event_id == ev["event_id"])).scalar()  # fmt: skip
            rows = [] if first is None else p.execute(select(quotes).where(
                quotes.c.event_id == ev["event_id"], quotes.c.polled_at == first)).all()
        qs = [Quote("nba", r.game_id, r.source, r.book, r.market, r.outcome, r.line, r.price,
                    r.player_name) for r in rows]  # fmt: skip
        rp = SimpleNamespace(margin_before=ev["margin_before"], margin_after=ev["margin_after"],
                             total_before=ev["total_before"], total_after=ev["total_after"],
                             props=list(ev["props"] or []))  # fmt: skip
        players = {ev["player_name"]: SimpleNamespace(player_name=ev["player_name"],
                                                      team_id=ev["team_id"],
                                                      new_status=ev["new_status"])}
        stated = utc(ev["report_at"] or ev["detected_at"])
        at = utc(first) if first is not None else utc(ev["detected_at"])
        reason = (f"[replay] {ev['player_name']} {ev['old_status'] or 'not listed'} -> "
                  f"{ev['new_status'] or 'not listed'}")
        ids = pol.signal_value("nba", ev["game_id"], ev["event_id"], f"replay{ev['event_id']}",
                               rp, qs, players, reason, stated, at, p_over)  # fmt: skip
        out["replayed"].append({"event_id": ev["event_id"], "player": ev["player_name"],
                                "change": f"{ev['old_status']} -> {ev['new_status']}",
                                "stated": stated.isoformat(), "detected": at.isoformat(),
                                "quotes_at_detection": len(qs), "alerts": len(ids)})
    grade_consensus(main, pdb, now)
    with pdb.connect() as p:
        new = p.execute(select(alerts).where(alerts.c.alert_id > before,
                                             alerts.c.signal == "value")).mappings().all()
        out["alerts"] = [{k: (v.isoformat() if isinstance(v, datetime) else v)
                          for k, v in a.items() if k in ("alert_id", "game_label", "market",
                                                         "player_name", "side", "line", "price",
                                                         "book", "fair_prob", "book_prob",
                                                         "reason")} for a in new]
        out["gates"] = gates(p)
    return out
