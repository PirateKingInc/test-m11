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
            props: document.querySelectorAll('section[data-tracker="nba"] li.prop').length,
            trigger: Array.from(document.querySelectorAll(
              'details[data-tonight="nba"] .trk-card .meta')).map(e => e.textContent),
            stored: (() => { try { return localStorage.getItem('betdata.nbaCard'); }
                             catch (e) { return 'err'; } })()};
  });
  const out = {start: await state()};
  await pg.click('[data-cardtoggle="nba"] button[data-pick="og"]');
  out.og = await state();
  await pg.reload();
  out.reloaded = await state();
  await pg.click('[data-cardtoggle="nba"] button[data-pick="spread"]');
  out.spread = await state();
  await pg.reload();
  out.spread_reloaded = await state();
  await pg.click('[data-cardtoggle="nba"] button[data-pick="action"]');
  out.action = await state();
  out.action_text = await pg.evaluate(() =>
    document.querySelector('details[data-tonight="nba"] .trk-cardbody').innerText);
  await pg.reload();
  out.action_reloaded = await state();
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
    # a Spread pick (owner 2026-11-10): shown only in the Spread view
    sp = {**g["picks"][0], "id": "sp1", "player": "", "team": "MIA", "opp": "ORL",
          "market": "Spread", "pick": "MIA +4.5", "odds": "-110", "card": False, "og": False,
          "spread": True, "label": "SPREAD", "stake": 1.0, "signal": "dog-spread",
          "trigger": "from Next Gen ML MIA +170", "bet": "1:spreads::home"}
    # an Action pick (owner 2026-11-12): only in the Action view, with stake and reason
    ac = {**g["picks"][0], "id": "ac1", "player": "Ivica Zubac", "market": "Rebounds",
          "pick": "under 9.5", "odds": "+116", "card": False, "og": False, "action": True,
          "action_stake": 0.25, "action_note": "missed: rebounds", "label": "",
          "action_posted": "live 8:15 AM ET",
          "action_og": "Would have made OG (posted after the morning card)",
          "signal": "F", "bet": "1:reb:zubac:under"}
    doc = {**TRACKER_DOC, "og_chosen": True, "spread_chosen": True, "action_chosen": True,
           "og_note": "1 pick today — OG found no other value",
           "card_note": "1 pick today — the engine found no other value",
           "games": [{**g, "picks": [both, og, sp, ac]}]}
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
    spr, spr2 = out["spread"], out["spread_reloaded"]
    act, act2 = out["action"], out["action_reloaded"]
    # default Next Gen with nothing stored
    assert start["official"] == ["nextgen"] and start["days"] == ["nextgen"]
    assert start["pressed"] == ["true", "false", "false", "false"] and start["stored"] is None
    assert start["tonight"].startswith("— Tonight: 1 card pick")
    # OG: every card-dependent section switches, and the choice is remembered
    assert og_["official"] == ["og"] and og_["days"] == ["og"]
    assert og_["pressed"] == ["false", "true", "false", "false"] and og_["stored"] == "og"
    assert og_["tonight"].startswith("— Tonight: 1 card pick")
    assert og_["both"] >= 1  # the same bet on both cards is marked so
    assert start["badges"] == 0 and og_["badges"] == 1  # the card badges follow the toggle
    assert again["official"] == ["og"] and again["days"] == ["og"], again  # after a reload
    assert back["official"] == ["nextgen"] and back["stored"] == "nextgen"
    # Spread (owner 2026-11-10): its own record and Results by day, Tonight's card shows its
    # pick with the triggering ML pick in small text; the choice persists; the Spread pick is
    # hidden from Next Gen and OG (two picks there, three in the Spread view)
    assert spr["official"] == ["spread"] and spr["days"] == ["spread"]
    assert spr["pressed"] == ["false", "false", "true", "false"] and spr["stored"] == "spread"
    assert spr["tonight"].startswith("— Tonight: 1 card pick")
    assert "from Next Gen ML MIA +170" in spr["trigger"]
    assert start["props"] == 3 and og_["props"] == 3 and spr["props"] == 4
    assert start["trigger"] == [] or all("Next Gen ML" not in t for t in start["trigger"])
    assert spr2["official"] == ["spread"] and spr2["stored"] == "spread"
    # Action (owner 2026-11-12): its own record and days, its picks with 0.25u and the reason
    assert act["official"] == ["action"] and act["days"] == ["action"]
    assert act["pressed"] == ["false", "false", "false", "true"] and act["stored"] == "action"
    assert act["tonight"].startswith("— Tonight: 1 card pick")
    assert "0.25u" in out["action_text"] and "missed: rebounds" in out["action_text"]
    assert "posted live 8:15 AM ET" in out["action_text"]
    assert "Would have made OG (posted after the morning card)" in out["action_text"]
    assert act2["official"] == ["action"] and act2["stored"] == "action"


TAB_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const exe = process.env.PW_CHROMIUM;
  const b = await chromium.launch(exe ? {executablePath: exe} : {});
  const ctx = await b.newContext({viewport: {width: 390, height: 900}});
  const pg = await ctx.newPage();
  const url = 'file://' + process.argv[2];
  const st = () => pg.evaluate(() => ({
    tab: Array.from(document.querySelectorAll('div.panel')).filter(p => !p.hidden)
      .map(p => p.id),
    sel: (document.querySelector('nav button[aria-selected=true]') || {}).dataset.tab,
    y: window.scrollY, card: window.betdataNbaCard}));
  const out = {};
  await pg.goto(url); out.fresh = await st();
  await pg.click('[data-cardtoggle="nba"] button[data-pick="og"]');
  await pg.click('nav button[data-tab="nfl"]'); out.clicked = await st();
  await pg.reload(); out.reloaded = await st();   // NFL is not remembered; OG is
  await pg.goto(url + '#alerts'); out.anchor = await st();
  console.log(JSON.stringify(out));
  await b.close();
})().catch(e => { console.error(e); process.exit(2); });
"""


def test_every_visit_opens_on_nba_and_anchors_still_work(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    import os

    from betdata.page.build import render
    from betdata.sports import ACTIVE, SportEntry

    tabs = [SportEntry(key="nfl", name="NFL", status=ACTIVE),
            SportEntry(key="nba", name="NBA", status=ACTIVE),
            SportEntry(key="research", name="Research", status=ACTIVE)]  # the live order
    from betdata.page.official import toggle_html

    nba = toggle_html("nextgen") + "<section data-card='nextgen'>n</section>" + \
        "<section data-card='og' hidden>o</section>" + "<p style='height:3000px'>long</p>"
    html = render(tabs, {"nfl": "<p>nfl</p>", "nba": nba,
                         "research": "<p style='height:2000px'>r</p><section id='alerts'>"
                                     "<h2>Alerts</h2></section>"},
                  {"last_ok_at": NOW.isoformat(), "missing_keys": [],
                   "detected_keys": []})  # fmt: skip
    page = tmp_path / "index.html"
    page.write_text(html)
    js = tmp_path / "tabs.js"
    js.write_text(TAB_SCRIPT)
    env = {**os.environ, **({"PW_CHROMIUM": _chromium()} if _chromium() else {})}
    r = subprocess.run([node, str(js), str(page)], capture_output=True, text=True, env=env,
                       timeout=120)  # fmt: skip
    if r.returncode and ("Cannot find module 'playwright'" in r.stderr
                         or "Executable doesn't exist" in r.stderr):  # fmt: skip
        pytest.skip("Playwright/Chromium not installed")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["fresh"]["tab"] == ["tab-nba"] and out["fresh"]["sel"] == "nba"
    assert out["fresh"]["y"] == 0  # at the top of the page
    assert out["clicked"]["tab"] == ["tab-nfl"]
    assert out["reloaded"]["tab"] == ["tab-nba"] and out["reloaded"]["y"] == 0  # not remembered
    assert out["reloaded"]["card"] == "og"  # the Next Gen | OG toggle still is
    assert out["anchor"]["tab"] == ["tab-research"] and out["anchor"]["y"] > 0  # #alerts


def test_parlay_block_on_the_next_gen_side_only(tmp_path, monkeypatch):
    """The parlay sits at the bottom of Tonight's card on the Next Gen side, with legs, book,
    odds, chance, stake, status and its own record; the OG side never shows it."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    import os

    import tests.runner.test_ui_freeze as F

    parlay = {"none": False, "kind": 2, "book": "FanDuel", "odds": "+220", "hits":
              "hits about 1 in 2", "stake": 0.25, "ev": 0.4, "result": None, "units": None,
              "record": "Parlay record: 0-0 · +0.00u · ROI – · 0 graded",
              "legs": [{"who": "Bam Adebayo (MIA) vs ORL", "text": "ORL @ MIA 7:30 PM ET: "
                        "Bam Adebayo over 20.5 points -110 · Next Gen 68% vs market 60%",
                        "result": None}]}  # fmt: skip
    monkeypatch.setattr(F, "TRACKER_DOC", {**TRACKER_DOC, "parlay": parlay})
    page = tmp_path / "index.html"
    page.write_text(build_fixture_page(tmp_path))
    js = tmp_path / "p.js"
    js.write_text(r"""
const { chromium } = require('playwright');
(async () => {
  const exe = process.env.PW_CHROMIUM;
  const b = await chromium.launch(exe ? {executablePath: exe} : {});
  const pg = await (await b.newContext({viewport: {width: 390, height: 900}})).newPage();
  await pg.goto('file://' + process.argv[2]);
  const t = () => pg.evaluate(() => {
    const f = document.querySelector('details[data-tonight="nba"] .trk-cardbody');
    const p = f.querySelector('.trk-parlay');
    return p ? {text: p.innerText, last: f.lastElementChild === p} : null; });
  const out = {ng: await t()};
  await pg.click('[data-cardtoggle="nba"] button[data-pick="og"]');
  out.og = await t();
  console.log(JSON.stringify(out)); await b.close();
})().catch(e => { console.error(e); process.exit(2); });
""")
    env = {**os.environ, **({"PW_CHROMIUM": _chromium()} if _chromium() else {})}
    r = subprocess.run([node, str(js), str(page)], capture_output=True, text=True, env=env,
                       timeout=120)  # fmt: skip
    if r.returncode and ("Cannot find module 'playwright'" in r.stderr
                         or "Executable doesn't exist" in r.stderr):  # fmt: skip
        pytest.skip("Playwright/Chromium not installed")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["og"] is None and out["ng"]["last"]
    text = out["ng"]["text"]
    for want in ("Parlay", "Bam Adebayo (MIA) vs ORL", "Next Gen 68% vs market 60%", "+220",
                 "FanDuel", "2 legs", "0.25u", "hits about 1 in 2", "OPEN",
                 "Parlay record: 0-0"):  # fmt: skip
        assert want.lower() in text.lower(), (want, text)


def test_news_alerts_box_fits_a_phone_like_the_other_sections(tmp_path):
    """The Research tab's News alerts box (owner 2026-11-09): same container width, padding and
    text size as the other sections, one stacked card per source, Sleeper spikes on their own
    line, and nothing overflowing at 390 px."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    import os

    from betdata.ledger import append_rows
    from betdata.news import ledger as NL
    from betdata.page.build import render
    from betdata.page.news import news_html
    from betdata.page.official import research_html
    from betdata.sports import ACTIVE, SportEntry

    led = tmp_path / "ledger"
    led.mkdir()
    rows = [{"key": f"k{i}", "source": src, "account": acct, "group": "national",
             "player": "Some Very Long Player Name", "ahead": "true", "outcome": out,
             "lead_min": "12.5" if out == "confirmed" else "", "corroborated": "false"}
            for i, (src, acct, out) in enumerate([
                ("bluesky", "averyveryverylonghandle-name.bsky.social", "confirmed"),
                ("bluesky", "omarisankofa.bsky.social", "official first"),
                ("sleeper", "trending adds", "no change"),
                ("sleeper", "trending adds", "confirmed")])]  # fmt: skip
    append_rows(led / NL.FILE, NL.FIELDS, rows, key="key")
    site = tmp_path / "site"
    research = research_html([], site) + news_html([], site, led)
    assert "<table" not in research and "Sleeper add spikes: 2 logged" in research
    tabs = [SportEntry(key="nba", name="NBA", status=ACTIVE),
            SportEntry(key="research", name="Research", status=ACTIVE)]
    html = render(tabs, {"nba": "<p>nba</p>", "research": research},
                  {"last_ok_at": NOW.isoformat(), "missing_keys": [], "detected_keys": []})
    page = tmp_path / "index.html"
    page.write_text(html)
    js = tmp_path / "n.js"
    js.write_text(r"""
const { chromium } = require('playwright');
(async () => {
  const exe = process.env.PW_CHROMIUM;
  const b = await chromium.launch(exe ? {executablePath: exe} : {});
  const pg = await (await b.newContext({viewport: {width: 390, height: 900}})).newPage();
  await pg.goto('file://' + process.argv[2]);
  await pg.click('nav button[data-tab="research"]');
  console.log(JSON.stringify(await pg.evaluate(() => {
    const box = s => { const e = document.querySelector(s), r = e.getBoundingClientRect(),
      c = getComputedStyle(e); return {w: r.width, l: r.left, pad: c.padding, fs: c.fontSize}; };
    const over = Array.from(document.querySelectorAll('section[data-news] *')).filter(
      e => e.getBoundingClientRect().right > window.innerWidth + 0.5).length;
    return {scroll: document.documentElement.scrollWidth, news: box('section[data-news]'),
            other: box('#tab-research section.research:not([data-news])'), over,
            cards: document.querySelectorAll('section[data-news] li.prop').length};
  })));
  await b.close();
})().catch(e => { console.error(e); process.exit(2); });
""")
    env = {**os.environ, **({"PW_CHROMIUM": _chromium()} if _chromium() else {})}
    r = subprocess.run([node, str(js), str(page)], capture_output=True, text=True, env=env,
                       timeout=120)  # fmt: skip
    if r.returncode and ("Cannot find module 'playwright'" in r.stderr
                         or "Executable doesn't exist" in r.stderr):  # fmt: skip
        pytest.skip("Playwright/Chromium not installed")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["scroll"] <= 390 and out["over"] == 0, out  # nothing overflows
    assert out["news"] == out["other"], out  # same width, position, padding, text size
    assert out["cards"] == 2  # one card per claim source; Sleeper is not one
