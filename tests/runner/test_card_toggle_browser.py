"""The "Next Gen | OG" toggle in a real browser (owner decision 2026-11-05): it switches every
card-dependent section (the record with its goal, Tonight's card, Results by day and the card
badges in Today) and remembers the viewer's choice. Runs where node and Playwright's Chromium
are installed (the cloud dev container); skipped elsewhere."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.runner.test_ui_freeze import NOW, TRACKER_DOC, build_fixture_page

SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const exe = process.env.PW_CHROMIUM;
  const b = await chromium.launch(exe ? {executablePath: exe} : {});
  const ctx = await b.newContext({viewport: {width: 390, height: 900}});
  const pg = await ctx.newPage();
  pg.clock && await pg.clock.setFixedTime(new Date(process.argv[3]));
  await pg.goto('file://' + process.argv[2]);
  const state = () => pg.evaluate(() => {
    const nba = document.querySelector('[data-cardtoggle="nba"]').closest('.panel') || document;
    const vis = s => Array.from(nba.querySelectorAll(s)).filter(e => !e.hidden).map(
      e => e.dataset.card);
    return {official: vis('section.official[data-card]'), days: vis('section.days[data-card]'),
            pressed: Array.from(nba.querySelectorAll('[data-cardtoggle] button')).map(
              x => x.getAttribute('aria-pressed')),
            tonight: (document.querySelector('details[data-tonight="nba"] .trk-sum')
                      || {}).textContent,
            badges: document.querySelectorAll('section[data-tracker="nba"] li .lbl.take').length,
            both: document.querySelectorAll('section[data-tracker="nba"] .lbl.both').length,
            stored: (() => { try { return localStorage.getItem('betdata.nbaCard'); }
                             catch (e) { return 'err'; } })()};
  });
  const out = {start: await state()};
  await pg.click('[data-cardtoggle="nba"] button[data-pick="og"]');
  out.og = await state();
  await pg.reload();
  out.reloaded = await state();
  await pg.click('[data-cardtoggle="nba"] button[data-pick="nextgen"]');
  out.back = await state();
  console.log(JSON.stringify(out));
  await b.close();
})().catch(e => { console.error(e); process.exit(2); });
"""


def _chromium() -> str | None:
    p = Path("/opt/pw-browsers/chromium")
    return str(p) if p.exists() else None


def test_toggle_switches_every_card_section_in_a_browser(tmp_path, monkeypatch):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    import tests.runner.test_ui_freeze as F

    # one pick on each card, the same bet on both (marked "on both cards")
    g = TRACKER_DOC["games"][0]
    both = {**g["picks"][0], "card": True, "og": False, "both": True, "bet": "1:reb:bam:over"}
    og = {**g["picks"][0], "id": "og1", "player": "Bam Adebayo", "card": False, "og": True,
          "og_label": "TAKE", "og_stake": 1.0, "both": True, "bet": "1:reb:bam:over",
          "signal": "F", "label": "LEAN"}
    doc = {**TRACKER_DOC, "og_chosen": True, "og_note": "1 pick today — OG found no other value",
           "card_note": "1 pick today — the engine found no other value",
           "games": [{**g, "picks": [both, og]}]}
    monkeypatch.setattr(F, "TRACKER_DOC", doc)
    page = tmp_path / "index.html"
    page.write_text(build_fixture_page(tmp_path))
    js = tmp_path / "toggle.js"
    js.write_text(SCRIPT)
    env = {"NODE_PATH": subprocess.run([node, "-e", "console.log(require.resolve.paths('x')"
                                        ".join(':'))"], capture_output=True,
                                       text=True).stdout.strip()}  # fmt: skip
    import os

    env = {**os.environ, **env, **({"PW_CHROMIUM": _chromium()} if _chromium() else {})}
    r = subprocess.run([node, str(js), str(page), NOW.isoformat()], capture_output=True,
                       text=True, env=env, timeout=120)  # fmt: skip
    if r.returncode and ("Cannot find module 'playwright'" in r.stderr
                         or "Executable doesn't exist" in r.stderr):  # fmt: skip
        pytest.skip("Playwright/Chromium not installed")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    start, og_, again, back = out["start"], out["og"], out["reloaded"], out["back"]
    # default Next Gen with nothing stored
    assert start["official"] == ["nextgen"] and start["days"] == ["nextgen"]
    assert start["pressed"] == ["true", "false"] and start["stored"] is None
    assert start["tonight"].startswith("— Tonight: 1 card pick")
    # OG: every card-dependent section switches, and the choice is remembered
    assert og_["official"] == ["og"] and og_["days"] == ["og"]
    assert og_["pressed"] == ["false", "true"] and og_["stored"] == "og"
    assert og_["tonight"].startswith("— Tonight: 1 card pick")
    assert og_["both"] >= 1  # the same bet on both cards is marked so
    assert start["badges"] == 0 and og_["badges"] == 1  # the card badges follow the toggle
    assert again["official"] == ["og"] and again["days"] == ["og"], again  # after a reload
    assert back["official"] == ["nextgen"] and back["stored"] == "nextgen"
