"""M20 UI freeze (MODEL.md "M20: UI freeze"): the main page (each sport tab: official record,
Today, Results by day, the card's picks, data sources; plus the page shell, styles and scripts)
keeps exactly the HTML structure it had before M20. Data values may differ; tags, ids, classes,
the order of elements, the styles and the scripts may not. Only the Research tab may change.

The snapshot `ui_freeze_snapshot.txt` was taken at main 92d65e5, before any M20 code. It is
never regenerated to make a change pass: a structural change to the main page needs the
owner's approval first (UI_FREEZE_REGENERATE=1 rewrites it, for that case only).

Owner-approved additions (each allowed exactly, and nothing else):
- 2026-11-03, the official success goal: one `div.goal` block per official-record header (plus
  an "All sports" block on the NBA tab) holding a progress line, a progress bar and the
  average-odds / break-even line. The test checks those blocks against GOAL_STRUCTURE, removes
  them, and compares the rest with the unchanged snapshot.
- 2026-11-04, "Tonight's card" and plain statuses (no "pending"), with the owner's layout
  answer: the official record stays at the top as a fold (folded by default, its preview the
  all-time record and the goal status), and "Tonight's card" is its own fold directly below it
  (open by default), ahead of Today's games. The snapshot was regenerated under this approval;
  the previous one is kept as `ui_freeze_snapshot_m19.txt` and the exact difference between
  them is pinned (APPROVED_DIFF_SHA). Today is drawn by the tracker renderer (page/tracker.py
  SCRIPT), pinned as TRACKER_SCRIPT_SHA (the M19 renderer was d866cf4bf364).
- 2026-11-04 (owner override: the NBA engine card), exactly the record lines: the NBA official
  record's "Engine card (since Nov 5)" heading above its periods, and "Card v1, nbap15 (Oct 31 –
  Nov 4)" as a folded line below them; the NBA goal block is the engine card's and "All sports"
  is labelled as combining both. The snapshot was regenerated under this approval; the previous
  one is kept as `ui_freeze_snapshot_m20b.txt` and the exact difference is pinned
  (APPROVED_DIFF_ENGINE_SHA). The Today renderer gained the short-card note
  ("2 picks today — the engine found no other value").
- 2026-11-05 (the 6:15 AM ET morning update), the ETA wording only: results after the last hourly
  run read "grading ~6:15 AM ET tomorrow" (and the card's selection time reads 6:15 AM ET). Text
  only: the snapshot is unchanged; the Today renderer's hash is the new approved version.
- 2026-11-05, two official NBA cards "Next Gen" and "OG": exactly a "Next Gen | OG" toggle (and
  its script) at the top of the NBA tab, a second official-record section and a second Results
  by day (one per card, the inactive one hidden), and the removal of the Engine-card heading,
  the folded Card v1 line and the combined "All sports" goal. The snapshot was regenerated
  under this approval; the previous one is kept as `ui_freeze_snapshot_m21.txt` and the exact
  difference is pinned (APPROVED_DIFF_TWO_CARDS_SHA), as are the toggle script and the Today
  renderer (which follows the toggle).
- 2026-11-05, NBA as the default tab: every visit opens on NBA (rendered selected, the other
  panels hidden), no tab is remembered, and a link with a section anchor (#alerts) opens the tab
  holding it. Exactly one line of the snapshot changes: the page shell's script hash
  (APPROVED_DIFF_DEFAULT_TAB_SHA; previous snapshot kept as `ui_freeze_snapshot_m22.txt`).
- 2026-11-06, the Next Gen parlay: a "Parlay" block at the bottom of Tonight's card on the Next
  Gen side, drawn by the Today renderer. The snapshot is unchanged; the renderer's approved
  hash moves (TRACKER_SCRIPT_SHA) for exactly this block.
- 2026-11-09, rules v2 records: in each NBA card's record, a "Rules v2 from Nov 10" marker
  (one <b>) and a "Since rules v2 (Nov 10)" row (one dt/dd with its record) next to the full
  record. Previous snapshot kept as `ui_freeze_snapshot_m24.txt`; the difference is pinned
  (APPROVED_DIFF_RULES_V2_SHA).
- 2026-11-10, the Spread Picks card: exactly a third toggle option ("Next Gen | OG | Spread",
  one more button and the toggle script's new version), the Spread card's official-record
  section (its periods and its 50-pick review line, no 500-pick goal) and its Results by day,
  both hidden by default. Previous snapshot kept as `ui_freeze_snapshot_m25.txt`; the difference
  is pinned (APPROVED_DIFF_SPREAD_SHA). The Today renderer's approved hash moves for the Spread
  view (Spread picks shown only there, the triggering ML pick in small text).
- 2026-11-11, OG Ranking v2: in the OG record only, a "Ranking v2 from Nov 12" marker (one <b>,
  styled as the rules v2 marker) and a "Since ranking v2 (Nov 12)" row (one dt/dd with its
  record). Previous snapshot kept as `ui_freeze_snapshot_m26.txt`; the difference is pinned
  (APPROVED_DIFF_RANKING_V2_SHA). The OG Top 3 shadow is never on the main page.
- 2026-11-12, the Action card: exactly a fourth toggle option ("Action", one more button and the
  toggle script's new version), the Action record section (its header line, periods and the "By
  reason" / "By market" lines) and its Results by day, both hidden by default; the Today renderer
  shows Action picks with their stake and "missed: <reason>" in the Action view, and each card's
  short / empty reason line (text). Previous snapshot kept as `ui_freeze_snapshot_m27.txt`; the
  difference is pinned (APPROVED_DIFF_ACTION_SHA).
- 2026-11-12, Action live adds (inside the Action view only): one "Morning and live adds" line
  (a p with its <b>) under the Action record — Morning / Live adds / Would have made OG — and,
  as text, each Action pick's posted time and the OG note (Today renderer and Results by day);
  threes appear in By market as data. Previous snapshot kept as `ui_freeze_snapshot_m28.txt`; the
  difference is pinned (APPROVED_DIFF_ACTION_ADDS_SHA).
"""

import hashlib
import os
import re
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path

from betdata import official as O
from betdata.page.build import RunInfo, et, render, sport_panel
from betdata.page.official import days_html, header_html
from betdata.page.tracker import tracker_html
from betdata.sports import ACTIVE, SportEntry
from tests.runner.dummy_sport import DummySport
from tests.runner.test_page_m19 import TODAY, _ledger

SNAPSHOT = Path(__file__).with_name("ui_freeze_snapshot.txt")
NOW = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)
TRACKER_DOC = {"sport": "nba", "generated_at": NOW.isoformat(), "games": [
    {"game_id": 1, "label": "ORL @ MIA", "tip": "2026-11-02T23:30:00+00:00", "state": "pre",
     "card": 1, "picks": [{"player": "Bam Adebayo", "team": "MIA", "opp": "ORL",
                           "market": "rebounds", "pick": "over 9.5", "odds": 140,
                           "book": "fanduel", "label": "LEAN", "card": True, "stake": 0.5,
                           "status": "Live"}]}]}


class _Structure(HTMLParser):
    """Element skeleton: tag, id and class (digits masked: ids carry dates and game ids), in
    document order. Text is data and dropped; <style> and code <script>s are kept as hashes;
    JSON <script>s (embedded data) are dropped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._in: str | None = None
        self._buf: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        keep = [f"{k}={re.sub(r'[0-9]+', '#', a[k] or '')}" for k in ("id", "class", "role")
                if k in a]
        self.out.append("  " * self.depth + " ".join([tag, *keep]))
        if tag in ("style", "script"):
            self._in = "json" if "json" in (a.get("type") or "") else tag
            self._buf = []
        if tag not in ("br", "meta", "link", "input", "img", "hr", "source"):
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in ("style", "script") and self._in:
            if self._in != "json":
                body = re.sub(r"\s+", " ", "".join(self._buf)).strip()
                self.out.append("  " * self.depth + f"<{tag} sha1 "
                                f"{hashlib.sha1(body.encode()).hexdigest()[:12]}>")
            self._in = None
        if tag not in ("br", "meta", "link", "input", "img", "hr", "source"):
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)


# the Today renderer's approved version (whitespace-normalised sha1, first 12 hex digits)
TRACKER_SCRIPT_HISTORY = {"d866cf4bf364": "M19 renderer (pre-2026-11-04)",
                          "0103d64f91b3": "Tonight's card fold, plain statuses (2026-11-04)",
                          "6405b2f92e7a": "engine card's short-card note (2026-11-04 override)",
                          "fbc70cba5745": "ETA wording: ~6:15 AM ET tomorrow (2026-11-05)",
                          "47458f1428a0": "Next Gen | OG toggle (2026-11-05)",
                          "7cbd8c4fbbca": "Next Gen parlay block (2026-11-06)",
                          "db5c65742992": "Spread view (2026-11-10)",
                          "007eb50fab39": "Action view (2026-11-12)",
                          "11c9addc71e2": "Action posted time and OG note (2026-11-12)"}
TRACKER_SCRIPT_SHA = "11c9addc71e2"
TOGGLE_SCRIPT_SHA = "6d68073346bb"  # the two-card toggle (2026-11-05)
TOGGLE_SCRIPT_SPREAD_SHA = "4f92a4d6e007"  # with the Spread option (2026-11-10)
TOGGLE_SCRIPT_ACTION_SHA = "af62d28260ec"  # with the Action option (2026-11-12)
SNAPSHOT_M19 = Path(__file__).with_name("ui_freeze_snapshot_m19.txt")
SNAPSHOT_M20B = Path(__file__).with_name("ui_freeze_snapshot_m20b.txt")
APPROVED_DIFF_SHA = "c1f5faf443d5"  # m19 snapshot -> m20b snapshot (2026-11-04)
APPROVED_DIFF_ENGINE_SHA = "22651c2d3629"  # m20b snapshot -> m21 (engine card, 2026-11-04)
SNAPSHOT_M21 = Path(__file__).with_name("ui_freeze_snapshot_m21.txt")
SNAPSHOT_M22 = Path(__file__).with_name("ui_freeze_snapshot_m22.txt")
SNAPSHOT_M24 = Path(__file__).with_name("ui_freeze_snapshot_m24.txt")
SNAPSHOT_M25 = Path(__file__).with_name("ui_freeze_snapshot_m25.txt")
APPROVED_DIFF_RULES_V2_SHA = "37e8a875c423"  # m24 -> m25 (rules v2 marker, 2026-11-09)
APPROVED_DIFF_SPREAD_SHA = "a11c511e0720"  # m25 -> m26 (Spread Picks, 2026-11-10)
SNAPSHOT_M26 = Path(__file__).with_name("ui_freeze_snapshot_m26.txt")
APPROVED_DIFF_RANKING_V2_SHA = "fb5b15b3460e"  # m26 -> m27 (OG Ranking v2, 2026-11-11)
SNAPSHOT_M27 = Path(__file__).with_name("ui_freeze_snapshot_m27.txt")
APPROVED_DIFF_ACTION_SHA = "aff0cd7d8a3e"  # m27 -> m28 (Action card, 2026-11-12)
SNAPSHOT_M28 = Path(__file__).with_name("ui_freeze_snapshot_m28.txt")
APPROVED_DIFF_ACTION_ADDS_SHA = "d0609dc7b83b"  # m28 -> current (Action live adds)
APPROVED_DIFF_DEFAULT_TAB_SHA = "d60079053e6b"  # m22 -> current (NBA default tab, 2026-11-05)
APPROVED_DIFF_TWO_CARDS_SHA = "bedb7949f2df"  # m21 snapshot -> current (two cards, 2026-11-05)
# the lines the engine-card approval added, exactly
ENGINE_ADDED = ["h3 class=era", "details class=fold era", "summary", "b", "span class=pv",
                "div class=foldbody", "p", "span class=rec", "a", "b", "a", "a"]
GOAL_BLOCK = re.compile(r"<div class='goal' data-goal='[^']*'[^>]*>.*?</div>", re.S)
# the exact skeleton of one goal block (owner-approved 2026-11-03)
GOAL_STRUCTURE = "div class=goal\n  p class=meta\n    b\n  progress\n  p class=meta\n"


def goal_blocks(html: str) -> list[str]:
    """The goal blocks inside the official-record headers (sport tabs only)."""
    main = re.sub(r'<div class="panel" id="tab-research".*?(?=<div class="panel" id="tab-|'
                  r'</main>|<footer)', "", html, flags=re.S)
    out = []
    for sec in re.findall(r"<section class='official'.*?</section>", main, flags=re.S):
        out += GOAL_BLOCK.findall(sec)
    return out


def main_page_structure(html: str, strip_goal: bool = True) -> str:
    """The page with the Research tab's panel (and the approved goal blocks) cut out, reduced
    to its skeleton."""
    if strip_goal:
        html = re.sub(r"(<section class='official'.*?</section>)",
                      lambda m: GOAL_BLOCK.sub("", m.group(1)), html, flags=re.S)
    html = re.sub(r'<div class="panel" id="tab-research".*?(?=<div class="panel" id="tab-|'
                  r'</main>|<footer)', "", html, flags=re.S)
    p = _Structure()
    p.feed(html)
    return "\n".join(p.out) + "\n"


def build_fixture_page(tmp_path: Path) -> str:
    led, site = tmp_path / "ledger", tmp_path / "site"
    _ledger(led)
    rows = O.dataset(led)
    today: date = TODAY
    entry = SportEntry(key="nba", name="NBA", status=ACTIVE,
                       plugin="tests.runner.dummy_sport:DummySport")
    sport = DummySport(entry)
    m19 = (header_html(rows, "nba", "NBA", today, site) + tracker_html("nba", TRACKER_DOC)
           + days_html(rows, "nba", today, site, et))
    info = RunInfo(last={"run_id": 1, "status": "ok", "details": {}}, last_ok=None,
                   rebuilt_at=None, today=[])
    research: list = []
    panel = sport_panel(sport, sport.cards(None, NOW), {}, sport.records(None), {}, info,
                        None, None, None, None, NOW, None, None, m19, research)
    panels = {"nba": panel, "research": "<p>research</p>" + "".join("".join(r) for r in research)}
    tabs = [entry, SportEntry(key="nfl", name="NFL", status=ACTIVE),
            SportEntry(key="research", name="Research", status=ACTIVE)]
    status = {"last_ok_at": NOW.isoformat(), "missing_keys": [], "detected_keys": []}
    return render(tabs, panels, status)


def test_main_page_structure_is_frozen(tmp_path):
    got = main_page_structure(build_fixture_page(tmp_path))
    if os.environ.get("UI_FREEZE_REGENERATE") == "1":  # owner-approved changes only
        SNAPSHOT.write_text(got)
    assert SNAPSHOT.exists(), "snapshot missing"
    assert got == SNAPSHOT.read_text(), (
        "The main page's HTML structure changed. M20 freezes it: only the Research tab may "
        "change (MODEL.md 'M20: UI freeze').")


def test_today_renderer_is_the_approved_version():
    from betdata.page.tracker import SCRIPT

    body = re.sub(r"\s+", " ", SCRIPT).strip()
    assert hashlib.sha1(body.encode()).hexdigest()[:12] == TRACKER_SCRIPT_SHA, (
        "The Today renderer (page/tracker.py SCRIPT) changed: a main-page change needs the "
        "owner's approval and a new TRACKER_SCRIPT_SHA here.")


def test_snapshot_differs_from_m19_by_exactly_the_approved_layout():
    """The regenerated snapshot differs from the M19 one only by the 2026-11-04 change: the
    official record wrapped in a fold, and the "Tonight's card" fold below it."""
    import difflib

    a = SNAPSHOT_M19.read_text().splitlines()
    b = SNAPSHOT_M20B.read_text().splitlines()
    d = "\n".join(difflib.unified_diff(a, b, lineterm="", n=0))
    assert hashlib.sha1(d.encode()).hexdigest()[:12] == APPROVED_DIFF_SHA, d
    added = [x[1:].strip() for x in d.splitlines() if x.startswith("+") and not x.startswith("+++")]
    assert "details class=fold" in added and "span class=pv trk-sum" in added
    assert "div class=foldbody trk-cardbody" in added


def test_snapshot_differs_by_exactly_the_engine_card_record_lines():
    """The engine-card approval (2026-11-04) added exactly the NBA record's two lines: the
    "Engine card" heading and the folded "Card v1" line."""
    import difflib

    a = SNAPSHOT_M20B.read_text().splitlines()
    b = SNAPSHOT_M21.read_text().splitlines()
    d = "\n".join(difflib.unified_diff(a, b, lineterm="", n=0))
    assert hashlib.sha1(d.encode()).hexdigest()[:12] == APPROVED_DIFF_ENGINE_SHA, d
    lines = [x for x in d.splitlines() if not x.startswith(("+++", "---", "@@"))]
    assert all(x.startswith("+") for x in lines), "the approval only adds lines"
    assert [x[1:].strip() for x in lines] == ENGINE_ADDED


def test_snapshot_differs_by_exactly_the_rules_v2_marker():
    """2026-11-09: per NBA card, the marker's <b> and one "Since rules v2" row."""
    import difflib

    a = SNAPSHOT_M24.read_text().splitlines()
    b = SNAPSHOT_M25.read_text().splitlines()
    d = "\n".join(difflib.unified_diff(a, b, lineterm="", n=0))
    assert hashlib.sha1(d.encode()).hexdigest()[:12] == APPROVED_DIFF_RULES_V2_SHA, d
    lines = [x for x in d.splitlines() if not x.startswith(("+++", "---", "@@"))]
    assert all(x.startswith("+") for x in lines)
    assert [x[1:].strip() for x in lines] == 2 * ["b", "dt", "dd", "span class=rec", "a", "b",
                                                  "a", "a"]


def test_snapshot_differs_by_exactly_the_spread_card():
    """2026-11-10: one toggle button, the toggle script's new version, the Spread record
    section and its Results by day; nothing removed but the old toggle script line."""
    import difflib

    a = SNAPSHOT_M25.read_text().splitlines()
    b = SNAPSHOT_M26.read_text().splitlines()
    d = "\n".join(difflib.unified_diff(a, b, lineterm="", n=0))
    assert hashlib.sha1(d.encode()).hexdigest()[:12] == APPROVED_DIFF_SPREAD_SHA, d
    added = [x[1:].strip() for x in d.splitlines() if x.startswith("+") and not x.startswith("+++")]
    removed = [x[1:].strip() for x in d.splitlines()
               if x.startswith("-") and not x.startswith("---")]  # fmt: skip
    assert removed == [f"<script sha1 {TOGGLE_SCRIPT_SHA}>"]
    assert added[:2] == ["button", f"<script sha1 {TOGGLE_SCRIPT_SPREAD_SHA}>"]
    assert added.count("section class=official") == 1 and added.count("section class=days") == 1
    assert "div class=goal" not in added  # its review line, not a 500-pick goal


def test_snapshot_differs_by_exactly_the_ranking_v2_marker(tmp_path):
    """2026-11-11: in the OG record only, the marker's <b> and one "Since ranking v2" row."""
    import difflib

    a = SNAPSHOT_M26.read_text().splitlines()
    b = SNAPSHOT_M27.read_text().splitlines()
    d = "\n".join(difflib.unified_diff(a, b, lineterm="", n=0))
    assert hashlib.sha1(d.encode()).hexdigest()[:12] == APPROVED_DIFF_RANKING_V2_SHA, d
    lines = [x for x in d.splitlines() if not x.startswith(("+++", "---", "@@"))]
    assert all(x.startswith("+") for x in lines)
    assert [x[1:].strip() for x in lines] == ["b", "dt", "dd", "span class=rec", "a", "b",
                                              "a", "a"]
    html = build_fixture_page(tmp_path)
    og = html[html.index("data-card='og'"):html.index("data-card='spread'")]
    assert "Ranking v2 from Nov 12" in og
    ng = html[html.index("data-card='nextgen'"):html.index("data-card='og'")]
    assert "Ranking v2 from Nov 12" in og and "Since ranking v2 (Nov 12)" in og
    assert "Ranking v2" not in ng


def test_snapshot_differs_by_exactly_the_action_card(tmp_path):
    """2026-11-12: one toggle button, the toggle script's new version, the Action record section
    and its Results by day; nothing removed but the old toggle script line."""
    import difflib

    a = SNAPSHOT_M27.read_text().splitlines()
    b = SNAPSHOT_M28.read_text().splitlines()
    d = "\n".join(difflib.unified_diff(a, b, lineterm="", n=0))
    assert hashlib.sha1(d.encode()).hexdigest()[:12] == APPROVED_DIFF_ACTION_SHA, d
    added = [x[1:].strip() for x in d.splitlines() if x.startswith("+") and not x.startswith("+++")]
    removed = [x[1:].strip() for x in d.splitlines()
               if x.startswith("-") and not x.startswith("---")]  # fmt: skip
    from collections import Counter

    # difflib pairs the new section with the similar Spread one, so compare net multisets
    net_add, net_del = Counter(added) - Counter(removed), Counter(removed) - Counter(added)
    assert net_del == Counter({f"<script sha1 {TOGGLE_SCRIPT_SPREAD_SHA}>": 1})
    assert net_add["button"] == 1 and net_add[f"<script sha1 {TOGGLE_SCRIPT_ACTION_SHA}>"] == 1
    assert net_add["section class=official"] == 1 and net_add["section class=days"] == 1
    assert "div class=goal" not in net_add
    html = build_fixture_page(tmp_path)
    assert "Action picks — not official. The best picks OG passed on." in html
    assert ">Action</button>" in html


def test_snapshot_differs_by_exactly_the_action_adds_line():
    """2026-11-12: one "Morning and live adds" line (p with b) inside the Action record."""
    import difflib

    a = SNAPSHOT_M28.read_text().splitlines()
    b = SNAPSHOT.read_text().splitlines()
    d = "\n".join(difflib.unified_diff(a, b, lineterm="", n=0))
    assert hashlib.sha1(d.encode()).hexdigest()[:12] == APPROVED_DIFF_ACTION_ADDS_SHA, d
    lines = [x for x in d.splitlines() if not x.startswith(("+++", "---", "@@"))]
    assert [x[1:].strip() for x in lines] == ["p class=meta", "b"]
    assert all(x.startswith("+") for x in lines)


def test_snapshot_differs_by_exactly_the_default_tab_script():
    """2026-11-05: NBA as the default tab changes only the page shell's script."""
    import difflib

    a = SNAPSHOT_M22.read_text().splitlines()
    b = SNAPSHOT_M24.read_text().splitlines()
    d = "\n".join(difflib.unified_diff(a, b, lineterm="", n=0))
    assert hashlib.sha1(d.encode()).hexdigest()[:12] == APPROVED_DIFF_DEFAULT_TAB_SHA, d
    changed = [x for x in d.splitlines() if x[:1] in "+-" and x[:3] not in ("+++", "---")]
    assert len(changed) == 2 and all("<script sha1" in x for x in changed)


def test_the_nba_tab_opens_by_default_whatever_the_tab_order():
    from betdata.page.build import render

    tabs = [SportEntry(key="nfl", name="NFL", status=ACTIVE),
            SportEntry(key="nba", name="NBA", status=ACTIVE),
            SportEntry(key="research", name="Research", status=ACTIVE)]  # the live order
    html = render(tabs, {"nfl": "<p>nfl</p>", "nba": "<p>nba</p>", "research": "<p>r</p>"},
                  {"last_ok_at": NOW.isoformat(), "missing_keys": [], "detected_keys": []})
    assert '<button role="tab" data-tab="nba" aria-selected=true>NBA</button>' in html
    assert '<button role="tab" data-tab="nfl">NFL</button>' in html
    assert '<div class="panel" id="tab-nba"><p>nba</p>' in html
    assert '<div class="panel" id="tab-nfl" hidden>' in html
    assert '<div class="panel" id="tab-research" hidden>' in html
    # no tab is remembered: the page never stores the tab
    script = html[html.rindex("<script>"):]
    assert "localStorage" not in script.split("var K='betdata-open'")[0]


def test_snapshot_differs_by_exactly_the_two_card_change():
    """2026-11-05: the toggle, the second record and Results by day sections, and nothing else
    (the Engine-card heading and the Card v1 fold removed)."""
    import difflib

    a = SNAPSHOT_M21.read_text().splitlines()
    b = SNAPSHOT_M22.read_text().splitlines()
    d = "\n".join(difflib.unified_diff(a, b, lineterm="", n=0))
    assert hashlib.sha1(d.encode()).hexdigest()[:12] == APPROVED_DIFF_TWO_CARDS_SHA, d
    added = [x[1:].strip() for x in d.splitlines() if x.startswith("+") and not x.startswith("+++")]
    removed = [x[1:].strip() for x in d.splitlines()
               if x.startswith("-") and not x.startswith("---")]  # fmt: skip
    assert added[:3] == ["div class=cardtoggle role=group", "button", "button"]
    assert added.count("section class=official") == 1 and added.count("section class=days") == 1
    assert "h3 class=era" in removed and "details class=fold era" in removed
    assert f"<script sha1 {TOGGLE_SCRIPT_SHA}>" in added


def test_every_card_dependent_section_follows_the_toggle(tmp_path):
    """The record (with its goal) and Results by day exist once per card, the OG ones hidden by
    default; the toggle script switches every [data-card] section and tells the Today renderer
    (Tonight's card and the Today badges), which reads the choice from localStorage."""
    from betdata.page.official import TOGGLE_JS
    from betdata.page.tracker import SCRIPT

    html = build_fixture_page(tmp_path)
    nba = html[html.index("id=\"tab-nba\""):html.index("id=\"tab-nfl\"")]
    assert nba.count("data-cardtoggle='nba'") == 1
    for kind in ("official", "days"):
        tags = re.findall(rf"<section class='{kind}' data-sport='nba'([^>]*)>", nba)
        assert tags == [" data-card='nextgen'", " data-card='og' hidden",
                        " data-card='spread' hidden", " data-card='action' hidden"], (kind, tags)
    assert nba.index("data-cardtoggle") < nba.index("<section class='official'")
    # the toggle: every [data-card] section, a remembered choice (try/catch), default Next Gen
    assert "querySelectorAll('[data-card]')" in TOGGLE_JS
    assert "localStorage.getItem(K)" in TOGGLE_JS and "localStorage.setItem(K" in TOGGLE_JS
    assert TOGGLE_JS.count("try {") == 3 and "root.dataset.default || 'nextgen'" in TOGGLE_JS
    assert "new CustomEvent('nbacard'" in TOGGLE_JS
    # Tonight's card and the Today badges follow it
    assert "addEventListener('nbacard'" in SCRIPT and "window.betdataNbaCard" in SCRIPT
    assert "on both cards" in SCRIPT


def test_goal_is_the_only_approved_addition(tmp_path):
    html = build_fixture_page(tmp_path)
    blocks = goal_blocks(html)
    # the NBA header: its own goal and "All sports"; nothing else was added
    assert [re.search(r"data-goal='([^']*)'", b).group(1) for b in blocks] == [
        "Next Gen", "OG"]
    for b in blocks:
        p = _Structure()
        p.feed(b)
        assert "\n".join(p.out) + "\n" == GOAL_STRUCTURE
    # a goal block anywhere else, or anything else added to the header, is still caught
    extra = html.replace("<h2>Today</h2>", "<h2>Today</h2><div class='goal' data-goal='x'>"
                         "<p class='meta'>x</p></div>")
    assert main_page_structure(extra) != main_page_structure(html)
    assert main_page_structure(html, strip_goal=False) != main_page_structure(html)


def test_research_tab_is_free_to_change(tmp_path):
    html = build_fixture_page(tmp_path)
    changed = html.replace("<p>research</p>", "<section class='x'><h2>NBA engine</h2></section>")
    assert main_page_structure(changed) == main_page_structure(html)
    # ...and a change on a sport tab is caught
    broken = html.replace("<h2>Today</h2>", "<h2>Today</h2><div class='new'></div>")
    assert main_page_structure(broken) != main_page_structure(html)
