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
  them, and compares the rest with the unchanged snapshot."""

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


def test_goal_is_the_only_approved_addition(tmp_path):
    html = build_fixture_page(tmp_path)
    blocks = goal_blocks(html)
    # the NBA header: its own goal and "All sports"; nothing else was added
    assert [re.search(r"data-goal='([^']*)'", b).group(1) for b in blocks] == ["NBA",
                                                                               "All sports"]
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
