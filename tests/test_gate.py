"""The Next Gen long-shot gate (owner decision 2026-11-09): besides its EV bar, a Next Gen pick
must beat the market's no-vig probability by 4 points up to +200, 7 points from +201 to +400,
10 points beyond +400; TAKE needs 1.5x that (6 / 10.5 / 15 points). OG is untouched."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from betdata.alerts import card as CD
from betdata.alerts import labels as LB
from betdata.alerts.db import alerts, engine_for, quotes
from betdata.nba.engine import picks as K


@pytest.mark.parametrize("price,pts", [(-500, 0.04), (-110, 0.04), (100, 0.04), (200, 0.04),
                                        (201, 0.07), (400, 0.07), (401, 0.10), (730, 0.10)])
def test_gate_tiers(price, pts):
    assert K.gate(price) == pts


def test_the_sac_example_needs_22_percent_at_730():
    assert not K.passes_gate(0.21, 0.12, 730) and K.passes_gate(0.22, 0.12, 730)
    assert not K.passes_gate(0.26, 0.12, 730, 1.5) and K.passes_gate(0.27, 0.12, 730, 1.5)


def _c(fair, pm, price, ev, player="P"):
    return K.Candidate("points", "over", 20.5, price, "draftkings", fair, pm, "consensus", fair,
                       ev, player, pm)  # fmt: skip


DESIGN = K.Design({}, {}, {"game": 0.05, "prop": 0.10}, {})


def test_pick_rule_needs_the_ev_bar_and_the_gate():
    big_ev_longshot = _c(0.20, 0.12, 730, 0.66, "A")  # EV +66% but only 8 points over market
    ok = _c(0.24, 0.12, 730, 0.99, "B")  # 12 points: clears the 10-point gate
    fav = _c(0.60, 0.565, -110, 0.145, "C")  # 3.5 points at -110: below 4
    fav_ok = _c(0.61, 0.565, -110, 0.165, "D")  # 4.5 points
    got = {c.player for c in K.best_picks([big_ev_longshot, ok, fav, fav_ok], DESIGN)}
    assert got == {"B", "D"}
    # the best qualifying candidate per market and player (a gated-out best EV gives way)
    a1 = _c(0.20, 0.12, 730, 0.66, "E")
    a2 = _c(0.62, 0.565, -110, 0.18, "E")
    assert [c.price for c in K.best_picks([a1, a2], DESIGN)] == [-110]


NOW = datetime(2026, 11, 10, 11, 15, tzinfo=UTC)  # Tue Nov 10, 6:15 AM ET
TIP = datetime(2026, 11, 11, 0, 30, tzinfo=UTC)


def _alert(fair, pm, price, ev, theta=0.10, tip=TIP, gate_details=True, signal="engine"):
    d = {"ev": ev, "theta": theta, "p_market": pm}
    if gate_details:
        d.update(edge_pp=fair - pm, gate=K.gate(price))
    return SimpleNamespace(signal=signal, details=d, tip=tip, game_id=7, market="points",
                           player_name="P", side="over", line=20.5, price=price,
                           book="draftkings", fair_prob=fair)  # fmt: skip


class _P:  # nothing stored: the price counts as available
    def execute(self, *a, **k):
        return SimpleNamespace(all=lambda: [])


def test_take_needs_ev_and_the_gate_at_1_5x():
    # EV 0.20 >= 1.5 x 0.10; edge 5 pts at -110: gate 4 ok, TAKE needs 6 -> LEAN
    assert LB.label(_P(), _alert(0.615, 0.565, -110, 0.20), NOW)["label"] == "LEAN"
    assert LB.label(_P(), _alert(0.63, 0.565, -110, 0.20), NOW)["label"] == "TAKE"  # 6.5 pts
    # a huge EV long shot: +730, 13 pts over the market: gate 10 ok, TAKE needs 15 -> LEAN
    assert LB.label(_P(), _alert(0.25, 0.12, 730, 1.07), NOW)["label"] == "LEAN"
    assert LB.label(_P(), _alert(0.28, 0.12, 730, 1.32), NOW)["label"] == "TAKE"
    # +201 to +400: TAKE needs 10.5 points
    assert LB.label(_P(), _alert(0.40, 0.30, 250, 0.40), NOW)["label"] == "LEAN"
    assert LB.label(_P(), _alert(0.41, 0.30, 250, 0.43), NOW)["label"] == "TAKE"
    # picks for games before the gate (already on a card) keep the old rule
    old = _alert(0.25, 0.12, 730, 1.07, tip=datetime(2026, 11, 10, 3, 0, tzinfo=UTC),
                 gate_details=False)  # a Nov 9 ET game
    assert LB.label(_P(), old, NOW)["label"] == "TAKE"


@pytest.fixture
def pdb(tmp_path):
    return engine_for(tmp_path / "poller.sqlite")


def _store(p, key, fair, pm, price, gate_details, signal="engine", player="P"):
    d = {"ev": fair * (1 + price / 100 if price > 0 else 1 + 100 / -price) - 1, "theta": 0.10,
         "p_market": pm}
    if gate_details:
        d.update(edge_pp=fair - pm, gate=K.gate(price))
    if signal != "engine":
        d = {"edge": 0.2, "edge_source": "news"}
    p.execute(insert(alerts).values(
        signal=signal, sport="nba", game_id=7, game_label="NYK @ BOS", tip=TIP,
        market="points", player_name=player, side="over", line=20.5, price=price,
        book="draftkings", fair_prob=fair, book_prob=pm, reason="r", dedup_key=key,
        created_at=NOW - timedelta(hours=8), quiet=True, sent=False, details=d))  # fmt: skip
    p.execute(insert(quotes).values(
        polled_at=NOW - timedelta(minutes=5), sport="nba", game_id=7, source="sharpapi",
        book="draftkings", market="points", player_name=player, outcome="over", line=20.5,
        price=price))  # fmt: skip


class _Main:
    def connect(self):
        import contextlib

        return contextlib.nullcontext(None)


def test_the_card_gates_picks_made_before_the_gate_shipped(pdb, monkeypatch):
    """Engine picks for Nov 10 games created before the gate (no gate details) are still
    gated at the 6:15 selection; OG's pool is untouched."""
    monkeypatch.setattr(CD, "lines_exist", lambda *a: True)
    with pdb.begin() as p:
        _store(p, "longshot", 0.20, 0.12, 730, False, player="A")  # 8 pts: out
        _store(p, "ok", 0.62, 0.565, -110, False, player="B")  # 5.5 pts: in (LEAN)
        _store(p, "og", 0.20, 0.12, 730, False, signal="snipe", player="C")  # OG: no gate
    [card] = CD.run(pdb, _Main(), NOW, lambda m, s, d: TIP if s == "nba" else None)
    assert [(x["dedup_key"], x["label"]) for x in card["picks"]] == [("ok", "LEAN")]
    [og] = CD.run_shadow(pdb, _Main(), NOW, lambda m, s, d: TIP if s == "nba" else None)
    assert [x["dedup_key"] for x in og["picks"]] == ["og"]
