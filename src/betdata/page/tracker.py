"""The "Today" section of each sport tab (M19, MODEL.md "M19: Page layout"; alerts/tracker.py
builds the data).

"Tonight's card" (owner request 2026-11-04), a fold directly below the official record, open
by default, its preview the summary line: one row per card pick (tip ET, game,
player (TEAM) vs OPP, pick and line, odds, book, label/stake, status), each jumping to its game
below, a summary line (picks, first tip, when results post) and a note that picks are locked at
the prices shown. A pick's status is plain words with an ETA, never "pending": "Tips 7:00 PM
ET", "In progress" (with the score when live scores are on), "Final — grading by ~9:15 PM ET"
(or "grading ~6:15 AM ET tomorrow"), then the result. Then today's games, each a collapsed
row with its time (ET), teams, status and score, and the number of card picks on it. Tapping
a game opens it: the card picks first, then "Other model picks (research, not official)"
grouped by team. Each pick: player (TEAM) vs OPP (or the team for
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
    """"Tonight's card" (a fold, open, directly below the official record), then Today."""
    data = json.dumps(doc or {"sport": sport, "games": []}).replace("</", "<\\/")
    return (f"<details class='fold' data-tonight='{escape(sport)}' open><summary><b>Tonight's "
            "card</b> <span class='pv trk-sum'></span></summary><div class='foldbody "
            "trk-cardbody'><p class='meta'>Loading…</p></div></details>"
            f"<section class='tracker' data-tracker='{escape(sport)}'><h2>Today</h2>"
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
  // the NBA tab's two official cards (owner 2026-11-05): the toggle sets window.betdataNbaCard
  function act(sport) {
    return sport === 'nba' && window.betdataNbaCard === 'og' ? 'og' : 'nextgen'; }
  function on(sport, p) { return act(sport) === 'og' ? !!p.og : !!p.card; }
  function badge(sport, p) {
    var og = act(sport) === 'og', c = on(sport, p);
    var lb = og && p.og ? p.og_label : p.label, st = og && p.og ? p.og_stake : p.stake;
    return (lb ? ' <span class="lbl ' + esc(lb.toLowerCase()) + '">' + esc(lb) +
      (c ? ' ' + st + 'u' : '') + '</span>' : '') +
      (p.both ? ' <span class="lbl both">on both cards</span>' : '');
  }
  function who(p) {
    if (p.player) return esc(p.player) + (p.team ? ' (' + esc(p.team) + ')' : '') +
      (p.opp ? ' vs ' + esc(p.opp) : '');
    return p.team ? esc(p.team) + (p.opp ? ' vs ' + esc(p.opp) : '') : 'Game total';
  }
  function pstat(g, p) {
    // a pick's status in plain words: tips / in progress / final and when it grades;
    // graded picks show their result instead (row)
    var now = Date.now();
    var ended = g.end && now >= Date.parse(g.end);
    if (g.state === 'final' || (ended && g.state !== 'live'))
      return '<span class="st final">Final — ' +
        esc(g.grade_text || 'result on the next hourly run') + '</span>';
    if (g.state === 'live' || (g.tip && now >= Date.parse(g.tip)))
      return '<span class="st inprog">In progress' + (g.score ? ' ' + esc(score(g)) : '') +
        '</span>';
    return '<span class="st sched">Tips ' + esc(tip(g.tip)) + '</span>' + (p.price ?
      ' <span class="st ' + esc(p.price.toLowerCase()) + '">' + esc(p.price) + '</span>' : '');
  }
  function result(p) {
    return '<div class="trk-res ' + esc(p.result) + '">' + esc(p.text) + '</div>' +
      (p.clv != null ? '<div class="meta">CLV ' + (p.clv > 0 ? '+' : '') + p.clv +
      ' pp</div>' : '');
  }
  function row(sport, g, p) {
    var line = p.result ? result(p) : '<div>' + pstat(g, p) + '</div>';
    return '<li class="prop"><div><b>' + who(p) + '</b> · ' + esc(p.market) + '</div><div>' +
      esc(p.pick) + ' <b>' + esc(p.odds) + '</b> at ' + esc(p.book) + badge(sport, p) + '</div>' +
      line + '<div class="meta">' + esc(p.signal) + '</div></li>';
  }
  function cardBlock(sport, games, d) {
    // "Tonight's card" (owner request 2026-11-04): every card pick, by tip; a row jumps to
    // its game below. A short engine card says so (owner override 2026-11-04).
    var rows = [];
    games.forEach(function (g) { g.picks.forEach(function (p) {
      if (on(sport, p)) rows.push([g, p]); }); });
    var txt = act(sport) === 'og' ? d.og_note : d.card_note;
    var note = txt ? '<p class="meta trk-note">' + esc(txt) + '.</p>' : '';
    if (!rows.length && note) return [txt, note];
    if (!rows.length) return ['none yet',
      '<p class="meta">No card picks today yet (the card is chosen at 6:15 AM ET).</p>'];
    rows.sort(function (a, b) { return Date.parse(a[0].tip) - Date.parse(b[0].tip); });
    var now = Date.now(), first = null, last = null, late = false, open_ = 0;
    rows.forEach(function (r) {
      var g = r[0], p = r[1];
      if (g.tip && Date.parse(g.tip) > now && first === null) first = g.tip;
      if (p.result) return;
      open_++;
      if (g.grade_at) { if (!last || Date.parse(g.grade_at) > Date.parse(last)) last = g.grade_at; }
      else late = true;
    });
    var n = rows.length, parts = ['Tonight: ' + n + ' card pick' + (n === 1 ? '' : 's')];
    if (first) parts.push('first tip ' + tip(first));
    if (!open_) parts.push('all graded');
    else if (last) parts.push('results by ~' + tip(last) + (late ?
      ' (late games ~6:15 AM ET tomorrow)' : ''));
    else if (late) parts.push('results ~6:15 AM ET tomorrow');
    return [parts.join(' · '), '<ul class="plist trk-card">' + rows.map(function (r) {
        var g = r[0], p = r[1];
        // three lines: when and which game, with the label; who and the pick (its line, odds
        // and book); then the status or the result
        var pk = p.pick || '';
        if (p.player && pk.indexOf(p.player) === 0) pk = pk.slice(p.player.length).trim();
        return '<li class="prop" role="button" tabindex="0" data-jump="' +
          esc(sport + '-' + g.game_id) + '"><div><b>' + esc(tip(g.tip)) + ' · ' +
          esc(g.label) + '</b>' + badge(sport, p) + '</div><div>' + who(p) + ' · ' + esc(pk) +
          ' <b>' + esc(p.odds) + '</b> at ' + esc(p.book) + '</div>' +
          (p.result ? result(p) : '<div>' + pstat(g, p) + '</div>') + '</li>';
      }).join('') + '</ul>' + note + '<p class="meta">Picks are locked at the prices shown. ' +
      'Results post on the hourly run after each game ends.</p>'];
  }
  function game(sport, g) {
    var card = g.picks.filter(function (p) { return on(sport, p); });
    var other = g.picks.filter(function (p) { return !on(sport, p); });
    var byTeam = {};
    other.forEach(function (p) {
      var k = p.team || 'Game'; (byTeam[k] = byTeam[k] || []).push(p); });
    var id = sport + '-' + g.game_id;
    var body = (card.length ? '<h4>Official (card) picks</h4><ul class="plist">' +
      card.map(function (p) { return row(sport, g, p); }).join('') + '</ul>' : '') +
      (other.length ? '<h4 class="research-head">Other model picks (research, not official)</h4>' +
      Object.keys(byTeam).sort().map(function (t) {
        return '<h5>' + esc(t) + '</h5><ul class="plist">' +
          byTeam[t].map(function (p) { return row(sport, g, p); }).join('') + '</ul>'; }).join('') :
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
    var cb = cardBlock(sport, games, d);
    var fold = document.querySelector('details[data-tonight="' + sport + '"]');
    if (fold) {
      fold.querySelector('.trk-sum').textContent = '— ' + cb[0];
      fold.querySelector('.trk-cardbody').innerHTML = cb[1];
    }
    sec.querySelector('.trk-body').innerHTML = games.length ?
      games.map(function (g) { return game(sport, g); }).join('') :
      '<p class="meta">No picks for today\\'s games yet.</p>';
    (fold ? fold.querySelectorAll('[data-jump]') : []).forEach(function (el) {
      function go() {
        var d = sec.querySelector('details[data-game="' + el.dataset.jump + '"]');
        if (!d) return;
        d.open = true; open[el.dataset.jump] = true;
        d.scrollIntoView({block: 'start', behavior: 'smooth'});
      }
      el.addEventListener('click', go);
      el.addEventListener('keydown', function (e) { if (e.key === 'Enter') go(); });
    });
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
  document.addEventListener('nbacard', function () { if (docs.nba) render('nba'); });
})();
</script>""".replace("%(repo)s", REPO)
