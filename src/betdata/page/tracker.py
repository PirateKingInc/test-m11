"""The "Today" section of each sport tab (M19, MODEL.md "M19: Page layout"; alerts/tracker.py
builds the data).

Today's games, each a collapsed row with its time (ET), teams, status and score, and the number
of official (card) picks on it. Tapping a game opens it: the card picks first, then "Other model
picks (research, not official)" grouped by team. Each pick: player (TEAM) vs OPP (or the team for
game markets), market, pick, line, odds, book, label, price status before the game, then the
result with the actual stat at the final. No prop is listed outside its game.

Rendered in the browser from a JSON document: the hourly build embeds today's copy, and the page
re-reads the poller's newer copy from the `tracker` branch every 2 minutes (GitHub's contents
API, then raw.githubusercontent.com) without a redeploy. Opened games stay open across refreshes.
"""

from __future__ import annotations

import json
from html import escape

REPO = "PirateKingInc/test-m11"


def tracker_html(sport: str, doc: dict | None) -> str:
    data = json.dumps(doc or {"sport": sport, "games": []}).replace("</", "<\\/")
    return (f"<section class='tracker' data-tracker='{escape(sport)}'><h2>Today</h2>"
            "<p class='meta trk-meta'></p><div class='trk-body'><p class='meta'>Loading…</p>"
            f"</div><script type='application/json' class='trk-data'>{data}</script></section>")


# The renderer, included once per page. Plain ES5.
SCRIPT = """<script>
(function(){
  var URLS = ['https://api.github.com/repos/%(repo)s/contents/tracker.json?ref=tracker',
              'https://raw.githubusercontent.com/%(repo)s/tracker/tracker.json'];
  var docs = {}, open = {};
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function tip(iso) { return iso ? new Date(iso).toLocaleTimeString('en-US', {
    timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit'}) + ' ET' : ''; }
  function ago(iso) { var m = Math.round((Date.now() - new Date(iso)) / 60000);
    return m < 1 ? 'just now' : m < 120 ? m + ' min ago' : Math.round(m / 60) + ' h ago'; }
  function score(g) { if (!g.score) return ''; var t = g.label.split(' @ ');
    return t[0] + ' ' + g.score.away + '–' + g.score.home + ' ' + t[1]; }
  function status(g) {
    if (g.state === 'final') return '<span class="st final">Final' +
      (g.score ? ' ' + esc(score(g)) : '') + '</span>';
    if (g.state === 'live') return '<span class="st inprog">Live' +
      (g.score ? ' ' + esc(score(g)) : '') +
      (g.period ? ' · ' + esc(g.period) + (g.clock ? ' ' + esc(g.clock) : '') : '') + '</span>';
    return '';
  }
  function badge(p) { return p.label ? ' <span class="lbl ' + esc(p.label.toLowerCase()) + '">' +
    esc(p.label) + (p.card ? ' ' + p.stake + 'u' : '') + '</span>' : ''; }
  function who(p) {
    if (p.player) return esc(p.player) + (p.team ? ' (' + esc(p.team) + ')' : '') +
      (p.opp ? ' vs ' + esc(p.opp) : '');
    return p.team ? esc(p.team) + (p.opp ? ' vs ' + esc(p.opp) : '') : 'Game total';
  }
  function row(g, p) {
    var line;
    if (p.result) line = '<div class="trk-res ' + esc(p.result) + '">' + esc(p.text) + '</div>' +
      (p.clv != null ? '<div class="meta">CLV ' + (p.clv > 0 ? '+' : '') + p.clv +
      ' pp</div>' : '');
    else if (g.state === 'pre') line = '<div><span class="st ' +
      esc((p.price || 'sched').toLowerCase()) + '">' + esc(p.price || 'No recent quote') +
      '</span></div>';
    else if (g.state === 'live') line = '<div><span class="st inprog">In progress</span></div>';
    else line = '<div class="meta">Final; result when the box score is in</div>';
    return '<li class="prop"><div><b>' + who(p) + '</b> · ' + esc(p.market) + '</div><div>' +
      esc(p.pick) + ' <b>' + esc(p.odds) + '</b> at ' + esc(p.book) + badge(p) + '</div>' +
      line + '<div class="meta">' + esc(p.signal) + '</div></li>';
  }
  function game(sport, g) {
    var card = g.picks.filter(function (p) { return p.card; });
    var other = g.picks.filter(function (p) { return !p.card; });
    var byTeam = {};
    other.forEach(function (p) {
      var k = p.team || 'Game'; (byTeam[k] = byTeam[k] || []).push(p); });
    var id = sport + '-' + g.game_id;
    var body = (card.length ? '<h4>Official (card) picks</h4><ul class="plist">' +
      card.map(function (p) { return row(g, p); }).join('') + '</ul>' : '') +
      (other.length ? '<h4 class="research-head">Other model picks (research, not official)</h4>' +
      Object.keys(byTeam).sort().map(function (t) {
        return '<h5>' + esc(t) + '</h5><ul class="plist">' +
          byTeam[t].map(function (p) { return row(g, p); }).join('') + '</ul>'; }).join('') :
      '');
    return '<details class="gamerow' + (card.length ? ' bet' : '') + '" data-game="' + esc(id) +
      '"' + (open[id] ? ' open' : '') + '><summary><b>' + esc(tip(g.tip)) + ' · ' +
      esc(g.label) + '</b> ' + status(g) + (card.length ? ' <span class="lbl take">★ ' +
      card.length + ' card</span>' : '') + ' <span class="pv">' + g.picks.length + ' pick' +
      (g.picks.length === 1 ? '' : 's') + '</span></summary><div class="foldbody">' + body +
      '</div></details>';
  }
  function render(sport) {
    var sec = document.querySelector('section[data-tracker="' + sport + '"]');
    var d = docs[sport]; if (!sec || !d) return;
    var games = d.games || [];
    var pend = 0;  // the only place pending picks are counted (never in or beside a record)
    games.forEach(function (g) { g.picks.forEach(function (p) {
      if (p.card && !p.result) pend++; }); });
    sec.querySelector('.trk-body').innerHTML = (pend ? '<p class="trk-pending"><b>Tonight: ' +
      pend + ' card pick' + (pend === 1 ? '' : 's') + ' pending</b></p>' : '') + (games.length ?
      games.map(function (g) { return game(sport, g); }).join('') :
      '<p class="meta">No picks for today\\'s games yet.</p>');
    sec.querySelectorAll('details[data-game]').forEach(function (el) {
      el.addEventListener('toggle', function () { open[el.dataset.game] = el.open; });
    });
    sec.querySelector('.trk-meta').textContent = 'Updated ' + ago(d.generated_at) +
      (d.scores ? ' · live scores: ' + d.scores : ' · live scores: off') +
      ' · player stats appear at the final. ★ = official card pick.';
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
  setInterval(function () { Object.keys(docs).forEach(render); }, 60000);
})();
</script>""".replace("%(repo)s", REPO)
