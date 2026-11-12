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
    # eligibility.py: pinned by function below since Rules v3 (owner 2026-11-12), which changes
    # only the official OG branch of `alert_reason` (file hash before Rules v3: d7a50d0b793b)
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


# The official OG card's selection (OG Ranking v2, owner 2026-11-11) and its Top 3 shadow, pinned
# before F threes / Action live adds / the OG + threes shadow (owner 2026-11-12).
OG_FUNCTIONS = {
    ("og_ranking", "reason"): "f63d66a1f0ee", ("og_ranking", "median_of"): "5c1c571e3f94",
    ("og_ranking", "_key"): "2de547fc038b", ("og_ranking", "order"): "d1829699966d",
    ("og_ranking", "pick_entry"): "7a43675c29a6", ("og_ranking", "ranked_pool"): "48f3c466a033",
    ("og_ranking", "card"): "361002ca24b4", ("og_ranking", "top3_of"): "1f7a1a087bb9",
    ("og_ranking", "f_fit"): "f60f81aa8e3b", ("og_ranking", "applies"): "182a72dccaa8",
    ("card", "decide_og"): "2a4d88bfa728", ("card", "choose_og"): "0e0a6672114a",
}  # fmt: skip


def test_official_og_selection_unchanged():
    from betdata import og_ranking

    mods = {"og_ranking": og_ranking, "card": card}
    got = {k: _h(inspect.getsource(getattr(mods[k[0]], k[1])).encode()) for k in OG_FUNCTIONS}
    assert got == OG_FUNCTIONS


# Rules v2 for Next Gen, the parlay and news (owner 2026-11-12, Rules v3): `reason()` and the rule
# helpers keep their exact source from before Rules v3; only `alert_reason`'s OG branch changed
# (rebound overs eligible on the official OG card from Nov 13), with the owner's approval.
ELIGIBILITY_FUNCTIONS = {"card_of": "33a8e116bdf3", "reason": "12f8bab4c181",
                         "applies": "bab68d5995df", "teammates_out": "b93a31f6b31f",
                         "_norm": "6ff08329ff34",
                         "alert_reason": "36ef92f4b0db"}  # Rules v3 (was 4b345a98ea4b)


def test_rules_v2_for_next_gen_unchanged():
    from betdata import eligibility as EL

    got = {n: _h(inspect.getsource(getattr(EL, n)).encode()) for n in ELIGIBILITY_FUNCTIONS}
    assert got == ELIGIBILITY_FUNCTIONS
    assert (EL.UNDER_LINE, EL.TEAMMATES_MAX, EL.OG_SIGNALS, EL.NEXTGEN_SIGNALS,
            EL.RULES_V2_START.isoformat()) == (5.5, 2, ("value", "snipe"), ("engine", "flash"),
                                               "2026-11-10")
