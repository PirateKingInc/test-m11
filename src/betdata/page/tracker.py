"""The "Today's tracker" section, directly below Tonight's card on each sport tab (owner request
2026-10-31; alerts/tracker.py builds the data).

The section is rendered in the browser from a JSON document: the hourly build embeds today's
document, and the page re-reads the poller's newer copy from the `tracker` branch every 2
minutes (GitHub's contents API, then raw.githubusercontent.com), without a redeploy. Games are
grouped with their tip time (ET) and status; the default view is the card's picks, and a toggle
shows all of today's picks (remembered in localStorage, wrapped in try/catch).
"""

from __future__ import annotations

import json
from html import escape

REPO = "PirateKingInc/test-m11"


def tracker_html(sport: str, doc: dict | None) -> str:
    data = json.dumps(doc or {"sport": sport, "games": []}).replace("</", "<\\/")
    return (f"<section class='tracker' data-tracker='{escape(sport)}'><h2>Today's tracker</h2>"
            "<p class='meta trk-meta'></p><div class='trk-body'><p class='meta'>Loading…</p>"
            f"</div><script type='application/json' class='trk-data'>{data}</script></section>")


# The renderer, included once per page. Plain ES5; every storage access is in try/catch.
SCRIPT = """<script>
(function(){
  var KEY = 'betdata-tracker-all', all = false;
  try { all = localStorage.getItem(KEY) === '1'; } catch (e) {}
  var URLS = ['https://api.github.com/repos/%(repo)s/contents/tracker.json?ref=tracker',
              'https://raw.githubusercontent.com/%(repo)s/tracker/tracker.json'];
  var docs = {};
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function tip(iso) { return iso ? new Date(iso).toLocaleTimeString('en-US', {
    timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit'}) + ' ET' : ''; }
  function ago(iso) { var m = Math.round((Date.now() - new Date(iso)) / 60000);
    return m < 1 ? 'just now' : m < 120 ? m + ' min ago' : Math.round(m / 60) + ' h ago'; }
  function score(g) { if (!g.score) return ''; var t = g.label.split(' @ ');
    return t[0] + ' ' + g.score.away + ' – ' + g.score.home + ' ' + t[1]; }
  function status(g) {
    if (g.state === 'final') return '<span class="st final">Final' +
      (g.score ? ' · ' + esc(score(g)) : '') + '</span>';
    if (g.state === 'live') return '<span class="st inprog">In progress' +
      (g.score ? ' · ' + esc(score(g)) : '') +
      (g.period ? ' · ' + esc(g.period) + (g.clock ? ' ' + esc(g.clock) : '') : '') + '</span>';
    return '<span class="st sched">Scheduled</span>';
  }
  function badge(p) { return p.label ? ' <span class="lbl ' + esc(p.label.toLowerCase()) + '">' +
    esc(p.label) + ' ' + p.stake + 'u</span>' : ''; }
  function row(g, p) {
    var head = '<b>' + esc(p.pick) + ' ' + esc(p.odds) + '</b> at ' + esc(p.book) + badge(p);
    var line;
    if (p.result) line = '<div class="trk-res ' + esc(p.result) + '">' + esc(p.text) + '</div>' +
      (p.clv != null ? '<div class="meta">CLV ' + (p.clv > 0 ? '+' : '') + p.clv +
      ' pp</div>' : '');
    else if (g.state === 'pre') line = p.price ? '<div><span class="st ' +
      esc(p.price.toLowerCase()) + '">' + esc(p.price) + '</span></div>' :
      '<div><span class="st sched">No recent quote</span></div>';
    else if (g.state === 'live') line = '<div><span class="st inprog">In progress</span></div>';
    else line = '<div class="meta">Final; result when the box score is in</div>';
    return '<li class="prop"><div>' + head + '</div>' + line + '<div class="meta">' +
      esc(p.signal) + (p.card ? ' · on the card' : '') + '</div></li>';
  }
  function render(sport) {
    var sec = document.querySelector('section[data-tracker="' + sport + '"]');
    var d = docs[sport]; if (!sec || !d) return;
    var games = d.games || [], total = 0, shown = 0, out = [];
    games.forEach(function (g) {
      var ps = g.picks.filter(function (p) { total++; return all || p.card; });
      shown += ps.length;
      if (!ps.length) return;
      out.push('<div class="trk-game"><h4>' + esc(g.label) + ' · ' + esc(tip(g.tip)) +
        '</h4><div>' + status(g) + '</div><ul class="plist">' +
        ps.map(function (p) { return row(g, p); }).join('') + '</ul></div>');
    });
    var btn = '<button type="button" class="trk-toggle" aria-pressed="' + all + '">' +
      (all ? 'Show card picks only' : 'Show all today\\'s picks (' + total + ')') + '</button>';
    var empty = !total ? '<p class="meta">No picks for today\\'s games yet.</p>' :
      !shown ? '<p class="meta">No card picks today.</p>' : '';
    sec.querySelector('.trk-body').innerHTML = btn + empty + out.join('');
    sec.querySelector('.trk-meta').textContent = 'Updated ' + ago(d.generated_at) +
      (d.scores ? ' · live scores: ' + d.scores : ' · live scores: off') +
      ' · player results appear at the final (no free live player stats).';
    sec.querySelector('.trk-toggle').onclick = function () {
      all = !all; try { localStorage.setItem(KEY, all ? '1' : '0'); } catch (e) {}
      Object.keys(docs).forEach(render);
    };
  }
  document.querySelectorAll('section[data-tracker]').forEach(function (sec) {
    try { docs[sec.dataset.tracker] = JSON.parse(sec.querySelector('.trk-data').textContent); }
    catch (e) {}
    render(sec.dataset.tracker);
  });
  function take(doc) {
    if (!doc || !doc.sports) return;
    Object.keys(doc.sports).forEach(function (s) {
      var d = doc.sports[s], cur = docs[s];
      if (cur && d.day === cur.day && (!cur.generated_at ||
          new Date(d.generated_at) > new Date(cur.generated_at))) docs[s] = d;
    });
    Object.keys(docs).forEach(render);
  }
  function get(i) {
    if (i >= URLS.length) return;
    var u = i ? URLS[i] + '?t=' + Date.now() : URLS[i];
    fetch(u, {cache: 'no-store', headers: i ? {} : {Accept: 'application/vnd.github.raw'}})
      .then(function (r) { if (!r.ok) throw r.status; return r.json(); })
      .then(take).catch(function () { get(i + 1); });
  }
  if (document.querySelector('section[data-tracker]') && !/^file:/.test(location.protocol)) {
    get(0);
    setInterval(function () { if (!document.hidden) get(0); }, 120000);
  }
  setInterval(function () { Object.keys(docs).forEach(render); }, 30000);
})();
</script>""".replace("%(repo)s", REPO)
