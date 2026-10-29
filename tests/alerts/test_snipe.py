"""M18 Part F: opening-line prop snipes. A new two-way DK/FD rebound or assist line is priced
by nbap15 at once; 6 pp edge, and other books at that line must not side against the model."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from betdata.alerts import snipe as S
from betdata.alerts.db import alerts, engine_for
from betdata.alerts.feeds import Quote
from betdata.alerts.poller import Keys, Poller
from betdata.moneyline import no_vig
from tests.nba.synth import synth

NOW = datetime(2021, 10, 20, 16, 0, tzinfo=UTC)


def prop(book, line, op, up, name="Plb Tma", market="rebounds", gid=1):
    return [Quote("nba", gid, "sharpapi", book, market, "over", line, op, name),
            Quote("nba", gid, "sharpapi", book, market, "under", line, up, name)]


def test_openers_of_needs_both_sides():
    ops = S.openers_of(prop("draftkings", 6.5, -110, -110) + prop("fanduel", 5.5, -120, 100)[:1]
                       + prop("draftkings", 20.5, -110, -110, market="points"))
    assert [(o.book, o.market, o.line) for o in ops] == [("draftkings", "rebounds", 6.5)]


def test_judge():
    op = S.Opener(1, "draftkings", "X", "rebounds", 6.5, -110, -110)
    s = S.judge(op, 0.58, None)
    assert s.side == "over" and s.edge == pytest.approx(0.08)
    assert S.judge(op, 0.55, None) is None  # 5 pp
    assert S.judge(op, 0.40, None).side == "under"
    # other books at 6.5 price the over at 48%: against the model -> no alert
    assert S.judge(op, 0.58, 0.48) is None
    assert S.judge(op, 0.58, 0.52).p_cons == pytest.approx(0.52)


def test_poller_snipes_a_new_line_once(engine, tmp_path, monkeypatch):
    gid = synth(engine, n_days=2)[0]
    pol = Poller(engine, engine_for(tmp_path / "poller.sqlite"), Keys("sk", None, None),
                 now_fn=lambda: NOW, lock=lambda: False)  # fmt: skip
    seen = []

    def price(rows, now):
        seen.extend(rows)
        return {(r["game_id"], "plbtma", r["market"], r["line"]):
                {"p_over": 0.60, "mean": 7.4, "dist": {"median": 7.0}, "line": r["line"]}
                for r in rows}  # fmt: skip

    monkeypatch.setattr(pol, "price_props", price)
    qs = prop("draftkings", 6.5, -110, -110, gid=gid)
    new = pol.store_quotes(qs, NOW, change_only=True)
    assert len(new) == 2
    ids = pol.snipes(gid, new, NOW)
    # the same line seen again is not a new opener
    assert pol.store_quotes(qs, NOW + timedelta(minutes=60), change_only=True) == []
    with pol.pdb.connect() as p:
        rows = p.execute(select(alerts)).all()
    assert len(ids) == 1 and len(rows) == 1
    a = rows[0]
    assert (a.signal, a.market, a.side, a.line, a.book) == ("snipe", "rebounds", "over", 6.5,
                                                            "draftkings")
    assert a.book_prob == pytest.approx(no_vig(-110, -110)[0]) and a.fair_prob == 0.60
    assert "median 7.0" in a.reason and seen[0]["line"] == 6.5
