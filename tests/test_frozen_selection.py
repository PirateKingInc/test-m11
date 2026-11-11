"""OG Ranking v2 (owner 2026-11-11) must leave Next Gen's selection, the Spread Picks card, the
parlay, the long-shot gate, Rules v2 and news alerts byte-for-byte unchanged. Their code is
pinned here at its state on main before the phase (sha1 of the source, first 12 hex digits);
the runner replay proof of Next Gen's stored cards is in the PR that split OG's path (#352).
A change here needs the owner's approval and a new hash."""

import hashlib
import inspect
from pathlib import Path

from betdata.alerts import card

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "src/betdata/spread.py": "207f1477bbb2",
    "src/betdata/parlay.py": "e9de07d78779",
    "src/betdata/eligibility.py": "d7a50d0b793b",
    "src/betdata/alerts/labels.py": "d33a412654be",
    "src/betdata/nba/engine/picks.py": "b4ab046e59bd",
    "src/betdata/calibration.py": "8517ee194c54",
    "src/betdata/news/__init__.py": "7e9d5cc30583",
    "src/betdata/news/accounts.py": "5f4d00ee2ed7",
    "src/betdata/news/claims.py": "734516850d0d",
    "src/betdata/news/cli.py": "07d5751be12a",
    "src/betdata/news/config.py": "b859662ef3b9",
    "src/betdata/news/desk.py": "207477fb0a6d",
    "src/betdata/news/ledger.py": "0921db40d209",
    "src/betdata/news/parse.py": "779f628abe07",
    "src/betdata/news/sources.py": "498d1c894dc8",
}  # fmt: skip
# Next Gen's (and NFL's) card selection in alerts/card.py
FUNCTIONS = {"decide": "11ae395da411", "choose": "1db641c31e34", "run": "66db1adeb025",
             "day_picks": "e1b3f9b04974", "ev": "fb52de77e4b0", "card_note": "106ddc98cb04",
             "message": "fe7ef83359d7", "push": "ec773a6f87af"}  # fmt: skip


def _h(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()[:12]


def test_frozen_files_unchanged():
    got = {f: _h((ROOT / f).read_bytes()) for f in FILES}
    assert got == FILES


def test_next_gen_selection_functions_unchanged():
    got = {n: _h(inspect.getsource(getattr(card, n)).encode()) for n in FUNCTIONS}
    assert got == FUNCTIONS


def test_og_uses_its_own_path():
    src = inspect.getsource(card.run_shadow)
    assert "decide_og(" in src and "decide(" not in src.replace("decide_og(", "")


def test_og_split_is_a_pure_refactor(tmp_path, monkeypatch):
    """Before the Ranking v2 date, `decide_og` returns exactly what `decide(..., V1_SIGNALS)`
    returned (lines -> the EV card; no lines -> wait, then the empty no_lines card)."""
    from datetime import timedelta

    from betdata import eligibility as EL
    from betdata.alerts.db import engine_for
    from tests.test_rules_v2 import NOW, TIP, _pick

    monkeypatch.setattr(EL, "teammates_out", lambda *a: 0)
    pdb = engine_for(tmp_path / "p.sqlite")
    with pdb.begin() as p:
        for i, (fair, price) in enumerate([(0.6, 110), (0.55, 150), (0.7, -120), (0.5, 200)]):
            _pick(p, f"k{i}", "snipe", "assists", "over", 4.5, fair=fair, price=price)
        _pick(p, "reb", "snipe", "rebounds", "over", 8.5)
    day = NOW.date()
    with pdb.connect() as p:
        for has in (True, False):
            for now in (NOW, TIP - timedelta(minutes=30)):
                lf = (lambda *a, h=has: h)  # noqa: E731
                old = card.decide(p, None, "nba", day, now, TIP, card.V1_SIGNALS, lf)
                new = card.decide_og(p, None, "nba", day, now, TIP, lf)
                assert new == old, (has, now)
