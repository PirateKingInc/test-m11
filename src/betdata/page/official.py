"""M19 page sections from the ledger (betdata/official.py; MODEL.md "M19: Page layout"):
- `header_html`: the official record (card picks): all time, last 7 days, yesterday;
- `days_html`: results by day, newest first, each day opening to its games and card picks with
  result, actual stat and reason tag, its tag counts and its CSV; a date field jumps to a day;
- `research_html`: each signal's research record and 200-pick gate (the Research tab).

Every record number links to a CSV of exactly the picks behind it (written under `site/csv/`).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

from betdata import official as O


def _units(u: float) -> str:
    return f"{u:+.2f}u"


def _roi(rec: O.Rec) -> str:
    return "–" if rec.roi is None else f"{100 * rec.roi:+.1f}%"


def _odds(rec: O.Rec) -> str:
    o = rec.avg_odds
    return "–" if o is None else (f"+{o}" if o > 0 else str(o))


def _clv(rec: O.Rec) -> str:
    return "–" if rec.clv is None else f"{rec.clv:+.1f} pp"


def stat_line(rec: O.Rec, href: str) -> str:
    """A record of graded picks only, like a team's: W-L-P (never without units), units, ROI,
    average odds, CLV and the number of graded picks. Each number links to those picks."""
    a = f"<a href='{escape(href)}'"
    return (f"<span class='rec' data-graded='{rec.graded}' data-w='{rec.wins}' "
            f"data-l='{rec.losses}' data-p='{rec.pushes}' data-units='{rec.units:.4f}'>"
            f"{a}><b>{rec.wlp}</b></a> · {a}>{_units(rec.units)}</a> · ROI {_roi(rec)} · "
            f"avg odds {_odds(rec)} · CLV {_clv(rec)} · {a}>{rec.graded} graded</a>"
            "</span>")  # fmt: skip


def header_html(rows: list[dict], sport: str, name: str, today: date, site: Path) -> str:
    """The official record, at the top of the tab and foldable (owner 2026-11-04): folded, its
    one-line preview gives the all-time record and the goal status; open, the periods and the
    goal block. The page's fold script remembers a reader who opens it.

    NBA (owner decision 2026-11-05): two official cards, "Next Gen" and "OG", each with its own
    record and goal; a toggle at the top of the tab shows one (`toggle_html`)."""
    if sport != "nba":
        return card_header(O.official(rows, sport), sport, name, today, site)
    from betdata import cards as C

    default = C.default()
    return toggle_html(default) + "".join(
        card_header(C.view(rows, k), sport, name, today, site, k, C.name(k), k != default)
        for k in nba_cards())  # fmt: skip


def nba_cards() -> tuple[str, ...]:
    """The NBA toggle's cards, in order: Next Gen, OG, and Spread Picks (owner 2026-11-10)
    while it is active (a retired Spread card keeps its ledger rows, not its toggle)."""
    from betdata import cards as C

    return (C.NEXTGEN, C.OG) + tuple(k for k in (C.SPREAD, C.ACTION)
                                     if k in C.load() and C.active(k))  # fmt: skip


ACTION_HEAD = "Action picks — not official. The best picks OG passed on."


def action_header(off: list[dict], sport: str, today: date, site: Path, label: str,
                  hidden: bool) -> str:  # fmt: skip
    """The Action card's record (owner decision 2026-11-12): not official, graded picks only,
    0.25u flat, with its record by reason tag and by market."""
    from betdata import action as AC

    per = O.periods(off, today)
    allt = per["All time"]
    preview = f"{label} {allt.wlp} · {_units(allt.units)} · not official"
    out = [f"<section class='official' data-sport='{escape(sport)}' data-card='action'"
           f"{' hidden' if hidden else ''}><details class='fold' data-fold='{escape(sport)}"
           f"-official'><summary><b>Record · NBA · {escape(label)}</b> <span "
           f"class='pv'>— {escape(preview)}</span></summary><div class='foldbody'><p "
           f"class='meta'><b>{escape(ACTION_HEAD)}</b> Up to 3 a day, 0.25u each, chosen with "
           "OG at 6:15 AM ET; each shows why OG did not make it official. Never part of OG's "
           "record. Graded picks only. Paper units, not advice.</p><dl class='periods'>"]
    for lab, rec in per.items():
        href = f"csv/{sport}-action-{lab.lower().replace(' ', '-')}.csv"
        O.write_csv(site / href, rec.rows)
        out.append(f"<dt>{escape(lab)}</dt><dd>{stat_line(rec, href)}</dd>")
    out.append("</dl>")
    for title, by in (("By reason", "reason"), ("By market", "market")):
        parts = AC.breakdown(off, by)
        txt = " · ".join(f"{k}: {r.wlp}, {_units(r.units)}" for k, r in parts.items())
        out.append(f"<p class='meta'><b>{title}</b>: {escape(txt or 'no graded picks yet')}</p>")
    out.append("</div></details></section>")
    return "".join(out)


def spread_header(off: list[dict], sport: str, today: date, site: Path, label: str,
                  hidden: bool) -> str:  # fmt: skip
    """The Spread Picks record (owner decision 2026-11-10): graded picks only, 1 unit flat;
    its own review at 50 graded picks instead of the 500-pick goal."""
    from betdata.spread import REVIEW_N

    per = O.periods(off, today)
    allt = per["All time"]
    preview = f"{label} {allt.wlp} · {_units(allt.units)} · review {allt.graded} of {REVIEW_N}"
    out = [f"<section class='official' data-sport='{escape(sport)}' data-card='spread'"
           f"{' hidden' if hidden else ''}><details class='fold' data-fold='{escape(sport)}"
           f"-official'><summary><b>Official record · NBA · {escape(label)}</b> <span "
           f"class='pv'>— {escape(preview)}</span></summary><div class='foldbody'><p "
           "class='meta'>One spread pick, 1 unit, on the same team as every Next Gen official "
           "card ML underdog, at the same book and price snapshot (chosen with Next Gen at 6:15 "
           "AM ET). Its own record: never in Next Gen's or OG's. Graded picks only; Nov 6-9 "
           "prices were reconstructed from stored quotes. Paper units, not advice.</p>"
           "<dl class='periods'>"]
    for lab, rec in per.items():
        href = f"csv/{sport}-spread-official-{lab.lower().replace(' ', '-')}.csv"
        O.write_csv(site / href, rec.rows)
        out.append(f"<dt>{escape(lab)}</dt><dd>{stat_line(rec, href)}</dd>")
    out.append(f"</dl><p class='meta'>Review at {REVIEW_N} graded picks: {allt.graded} of "
               f"{REVIEW_N} (owner 2026-11-10; nothing turns off)</p>"
               "</div></details></section>")
    return "".join(out)


V2_MARKER = "Rules v2 from Nov 10"
V2_LABEL = "Since rules v2 (Nov 10)"
RANK_MARKER = "Ranking v2 from Nov 12"  # OG only (owner 2026-11-11, MODEL.md "OG Ranking v2")
RANK_LABEL = "Since ranking v2 (Nov 12)"


def card_header(off: list[dict], sport: str, name: str, today: date, site: Path,
                card: str | None = None, label: str = "", hidden: bool = False) -> str:
    """One card's official record header: periods and its own success goal."""
    if card == "spread":
        return spread_header(off, sport, today, site, label, hidden)
    if card == "action":
        return action_header(off, sport, today, site, label, hidden)
    per = O.periods(off, today)
    allt, g = per["All time"], O.goal(off)
    v2 = None
    if card:  # rules v2 (owner 2026-11-09): the record since the marker, beside the full one
        from betdata.eligibility import RULES_V2_START

        since = RULES_V2_START.isoformat()
        v2 = O.record([r for r in off if r["day"] >= since], O.official_stake)
        per = {"All time": per["All time"], V2_LABEL: v2,
               **{k: v for k, v in per.items() if k != "All time"}}
    rank = None
    if card == "og":  # OG Ranking v2 (owner 2026-11-11): its record since the marker
        from betdata.og_ranking import start

        rank = O.record([r for r in off if r["day"] >= start().isoformat()], O.official_stake)
        per = {k: v for k, v in [*list(per.items())[:2], (RANK_LABEL, rank),
                                 *list(per.items())[2:]]}
    preview = (f"{label + ' ' if card else 'All time '}{allt.wlp} · {_units(allt.units)} · "
               + (f"rules v2 {v2.wlp} · " if v2 is not None else "")
               + f"goal {g.graded} of {O.GOAL_N}, {g.status}")
    tag = f"-{card}" if card else ""
    attrs = (f" data-card='{escape(card)}'" if card else "") + (" hidden" if hidden else "")
    out = [f"<section class='official' data-sport='{escape(sport)}'{attrs}>"
           f"<details class='fold' data-fold='{escape(sport)}-official'><summary><b>Official "
           f"record · {escape(name)}{f' · {escape(label)}' if card else ''}</b> <span "
           f"class='pv'>— {escape(preview)}</span></summary><div class='foldbody'><p "
           "class='meta'>Official picks are the daily card (up to 5, chosen at 6:15 AM ET). "
           "Like a team's record, graded picks only; today's picks are under Tonight's card "
           "until they are graded. Paper units, not advice."
           + (f" <b>{escape(V2_MARKER)}</b>: card eligibility rules v2 (the goal continues on "
              "the full record)." if card else "")
           + (f" <b>{escape(RANK_MARKER)}</b>: OG ranks its picks by the model's win probability "
              "(-150 to +130, the model's median on the bet's side; the full record continues)."
              if rank is not None else "") + "</p><dl class='periods'>"]
    for lab, rec in per.items():
        slug = ("since-rules-v2" if lab == V2_LABEL else "since-ranking-v2" if lab == RANK_LABEL
                else lab.lower().replace(" ", "-"))
        href = f"csv/{sport}{tag}-official-{slug}.csv"
        O.write_csv(site / href, rec.rows)
        out.append(f"<dt>{escape(lab)}</dt><dd>{stat_line(rec, href)}</dd>")
    out.append("</dl>")
    out.append(goal_html(off, label or name))
    out.append("</div></details></section>")
    return "".join(out)


def toggle_html(default: str) -> str:
    """"Next Gen | OG | Spread" at the top of the NBA tab (owner decisions 2026-11-05 and, for
    Spread, 2026-11-10): it switches the
    card block, the official record with its goal, Results by day and the card badges in Today.
    Each viewer's choice is remembered in localStorage (default Next Gen)."""
    from betdata import cards as C

    btn = "".join(f"<button type='button' data-pick='{k}' aria-pressed='"
                  f"{'true' if k == default else 'false'}' style='flex:1;padding:8px;"
                  "border:0;border-radius:8px;font:inherit;font-weight:600;cursor:pointer'>"
                  f"{escape(TOGGLE_NAMES.get(k, C.name(k)))}</button>" for k in nba_cards())
    return (f"<div class='cardtoggle' role='group' data-cardtoggle='nba' data-default='"
            f"{escape(default)}' style='display:flex;gap:6px;margin:10px 0;padding:4px;"
            f"border:1px solid var(--line);border-radius:10px'>{btn}</div>{TOGGLE_JS}")


TOGGLE_NAMES = {"spread": "Spread", "action": "Action"}  # short toggle labels
TOGGLE_JS = """<script>
(function(){
  var K = 'betdata.nbaCard';
  var root = document.querySelector('[data-cardtoggle="nba"]');
  if (!root) return;
  var scope = root.closest('.panel') || document;
  function get() {
    try { var v = localStorage.getItem(K);
      if (v === 'og' || v === 'nextgen' || ((v === 'spread' || v === 'action') &&
        root.querySelector('[data-pick="' + v + '"]'))) return v; }
    catch (e) {}
    return root.dataset.default || 'nextgen';
  }
  function apply(v) {
    scope.querySelectorAll('[data-card]').forEach(function (el) {
      el.hidden = el.dataset.card !== v; });
    root.querySelectorAll('button').forEach(function (b) {
      var on = b.dataset.pick === v;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.style.background = on ? 'var(--accent, #0a7)' : 'transparent';
      b.style.color = on ? '#fff' : 'inherit';
    });
    window.betdataNbaCard = v;
    try { document.dispatchEvent(new CustomEvent('nbacard', {detail: v})); } catch (e) {}
  }
  root.querySelectorAll('button').forEach(function (b) {
    b.addEventListener('click', function () {
      try { localStorage.setItem(K, b.dataset.pick); } catch (e) {}
      apply(b.dataset.pick);
    });
  });
  window.betdataNbaCard = get();  // read by the Today renderer on its first draw
  // the sections it switches come after this script: apply once the page is parsed
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { apply(get()); });
  } else { apply(get()); }
})();
</script>"""


def goal_html(rows: list[dict], label: str, note: str = "") -> str:
    """The official success goal's progress line (owner decision 2026-11-03): the goal and the
    card's progress, a progress bar to 500 graded picks, the status, and the card's average
    odds with the break-even win rate."""
    g = O.goal(rows)
    pct = "" if g.pct is None else f"{g.pct:.4f}"
    return (f"<div class='goal' data-goal='{escape(label)}' data-graded='{g.graded}' "
            f"data-pct='{pct}' data-units='{g.units:.4f}' data-status='{escape(g.status)}'>"
            f"<p class='meta'><b>{escape(label)}</b>{f' ({escape(note)})' if note else ''} · "
            f"{escape(O.goal_text(g))}</p>"
            f"<progress max='{O.GOAL_N}' value='{min(g.graded, O.GOAL_N)}' "
            f"style='width:100%'>{g.graded} of {O.GOAL_N}</progress>"
            f"<p class='meta'>{escape(O.context_text(g))}</p></div>")


def _tags(rows: list[dict]) -> str:
    """'Losses: 4 Minutes miss, 1 Variance · Wins: ...' (MODEL.md "M19": reason tags)."""
    tc = O.tag_counts(rows)
    parts = []
    for res, word in (("loss", "Losses"), ("win", "Wins")):
        d = tc[res]
        n = sum(d.values())
        if n:
            parts.append(f"{word} ({n}): " + ", ".join(
                f"{v} {escape(k)}" for k, v in sorted(d.items(), key=lambda kv: -kv[1])))
    return " · ".join(parts) or "No reason tags yet."


def ungraded_text(r: dict, now: datetime | None = None) -> str:
    """A pick without a result, in plain words with an ETA (alerts/tracker.plain_status)."""
    from datetime import UTC

    from betdata.alerts.tracker import plain_status

    tip = datetime.fromisoformat(r["tip"]) if r.get("tip") else None
    return plain_status(tip, r.get("sport") or "nba", now or datetime.now(UTC))


def _result_cell(r: dict, now: datetime | None = None) -> str:
    res = r.get("result") or ""
    if not res:
        return f"<span class='st sched'>{escape(ungraded_text(r, now))}</span>"
    u = O.csv_row(r)["units"]
    stat = r.get("actual_stat")
    stat_txt = "" if not stat else f" · actual {escape(stat)}"
    if res == "void":
        return "<span class='st moved'>VOID</span>"
    cls = {"win": "live", "loss": "gone"}.get(res, "moved")
    return (f"<span class='st {cls}'>{escape(res.upper())} {float(u):+.2f}u</span>"
            f"{stat_txt}")


def pick_line(r: dict) -> str:
    """Player (TEAM) vs OPP, or the team for game markets; market; pick; odds; book; label."""
    from betdata.alerts.notify import BOOKS, MARKETS, _fmt_price

    who = r["player"] or r.get("team") or ""
    team, opp = r.get("team"), r.get("opponent")
    if r["player"]:
        who = f"{r['player']} ({team})" if team else r["player"]
        if opp:
            who += f" vs {opp}"
    elif team and opp:
        who = f"{team} vs {opp}"
    lab = r.get("card_label") if r["official"] else r.get("label")
    stake = O.official_stake(r) if r["official"] else None
    badge = "" if not lab else (f" <span class='lbl {escape(lab.lower())}'>{escape(lab)}"
                                f"{f' {stake:g}u' if stake else ''}</span>")
    return (f"<div><b>{escape(who)}</b> · {escape(MARKETS.get(r['market'], r['market']))}"
            f"</div><div>{escape(O.pick_text(r))} <b>{escape(_fmt_price(int(r['odds'])))}</b>"
            f" at {escape(BOOKS.get(r['book'], r['book']))}{badge}</div>")


def void_line(rs: list[dict]) -> str:
    """'1 void: Jalen Suggs (ORL) did not play' (the day view only; never in a record)."""
    vs = O.voids(rs)
    if not vs:
        return ""
    names = []
    for r in vs:
        who = f"{r['player']} ({r['team']})" if r["player"] and r.get("team") else r["player"]
        names.append(f"{who} did not play" if r["player"] else f"{r['game']}: no action")
    return (f"<p class='meta voids'>{len(vs)} void{'s' if len(vs) != 1 else ''}: "
            f"{escape('; '.join(names))}</p>")


def days_html(rows: list[dict], sport: str, today: date, site: Path, et) -> str:
    """Results by day; on the NBA tab one per card (Next Gen, OG), shown by the toggle."""
    if sport != "nba":
        return card_days_html(rows, O.official(rows, sport), sport, today, site, et)
    from betdata import cards as C

    both = C.on_both(rows)
    default = C.default()
    return "".join(card_days_html(rows, C.view(rows, k), sport, today, site, et, k,
                                  k != default, both) for k in nba_cards())


def card_days_html(rows: list[dict], off: list[dict], sport: str, today: date, site: Path, et,
                   card: str | None = None, hidden: bool = False,
                   both: set | None = None) -> str:  # fmt: skip
    from betdata import cards as C

    days = O.by_day(off)
    tag = f"-{card}" if card else ""
    week = [r for r in off if r["day"] >= (today - timedelta(days=6)).isoformat()]
    attrs = (f" data-card='{escape(card)}'" if card else "") + (" hidden" if hidden else "")
    out = [f"<section class='days' data-sport='{escape(sport)}'{attrs}><h2>Results by day</h2>"
           f"<p class='meta'>Last 7 days, why picks won or lost: {_tags(week)}</p>"
           f"<label class='jump'>Jump to a day <input type='date' class='dayjump' "
           f"min='{escape(min(days) if days else '')}' max='{escape(today.isoformat())}'>"
           "</label>"]
    if not days:
        out.append("<p class='meta'>No card picks yet.</p>")
    for d, rs in days.items():
        rec = O.record(rs, O.official_stake)
        href = f"csv/{sport}{tag}-day-{d}.csv"
        src = rs if card else [r for r in rows if r["sport"] == sport and r["day"] == d]
        O.write_csv(site / href, [r for r in src if r.get("result")])  # graded and void
        label = datetime.fromisoformat(d).strftime("%a %b %-d")
        games: dict[str, list] = {}
        for r in O.graded(rs):
            games.setdefault(r["game"], []).append(r)
        body = []
        for g, ps in games.items():
            tip = et(datetime.fromisoformat(ps[0]["tip"]), "%-I:%M %p")
            body.append(f"<div class='trk-game'><h4>{escape(g)} · {escape(tip)}</h4>"
                        "<ul class='plist'>" + "".join(
                            f"<li class='prop'>{pick_line(r)}<div>{_result_cell(r)}</div>"
                            f"<div class='meta'>{escape(r['signal_name'])}"
                            + (f" · why: <b>{escape(r['review_tag'])}</b>"
                               if r.get("review_tag") else "")
                            + (" · <b>on both cards</b>" if both and C.both_mark(r, both)
                               and card != C.SPREAD else "")
                            + (f" · missed: <b>{escape(r.get('action_reason', ''))}</b>"
                               if card == C.ACTION else "")
                            + (" · <b>reconstructed</b>" if card == C.SPREAD
                               and r.get("spread_backfill") else "")
                            + "</div></li>" for r in ps) + "</ul></div>")  # fmt: skip
        out.append(f"<details class='day' id='{escape(sport)}{tag}-day-{escape(d)}' "
                   f"data-day='{escape(d)}'><summary><b>{escape(label)}</b> "
                   f"<span class='pv'>{rec.wlp} · {_units(rec.units)} · CLV {_clv(rec)}"
                   f"</span></summary><div class='foldbody'>{stat_line(rec, href)}"
                   f"<p class='meta'>{_tags(rs)}</p>{''.join(body)}{void_line(rs)}"
                   "<p class='meta'>"
                   f"<a href='{escape(href)}'>Download {escape(label)} (CSV, every graded pick "
                   "of the day)</a></p></div></details>")  # fmt: skip
    out.append("</section>")
    return "".join(out)


def research_html(rows: list[dict], site: Path) -> str:
    """Each signal's research record (all its picks, graded only, flat 1 unit), its three
    checks (M20 Part A) and the earlier M18 CLV gate."""
    from betdata.alerts.gates import GATE_N, gate
    from betdata.checks import ARCHIVED, N_WORKS, RETIRED, picks_of, summary_line, three_checks

    out = ["<section class='research'><h2>Signals (research, not official picks)</h2>"
           "<p class='meta'>Every graded pick of each signal, card or not, at a flat 1 unit. "
           f"Since M20 a signal works only if three checks pass over {N_WORKS} graded picks: "
           "calibration, a Brier score closer than the closing no-vig line, and a positive "
           "EV at the close (closing no-vig × odds taken − 1), each with a 95% range.</p>"
           "<ul class='plist'>"]
    for name, rec in O.signals(rows).items():
        href = f"csv/signal-{name}.csv"
        O.write_csv(site / href, rec.rows)
        g = gate(rec.clvs)
        ck = three_checks(picks_of(rec.rows))
        retired = (f" <span class='meta'>retired {escape(RETIRED[name])}: no new picks"
                   "</span>" if name in RETIRED else "")  # fmt: skip
        if name in ARCHIVED:
            retired = (f" <span class='meta archived'>feeds the OG card (archived "
                       f"{escape(ARCHIVED[name])}, official again 2026-11-05); picks recorded, "
                       "not pushed one by one</span>")
        out.append(f"<li class='prop' data-signal='{escape(name)}'><div><b>{escape(name)}</b>"
                   f"{retired}</div><div>{stat_line(rec, href)}</div>"
                   f"<div class='meta' data-checks='{escape(ck.status)}'>Three checks: "
                   f"{escape(summary_line(ck))}</div><div class='meta'>M18 CLV gate: "
                   f"{g['n']}/{GATE_N} graded with CLV · {escape(g['status'])}</div></li>")
    out.append("</ul></section>")
    return "".join(out)


JUMP_JS = """<script>
(function(){
  document.querySelectorAll('input.dayjump').forEach(function (inp) {
    inp.addEventListener('change', function () {
      var sec = inp.closest('section'), sport = sec.dataset.sport;
      var card = sec.dataset.card ? '-' + sec.dataset.card : '';
      var d = document.getElementById(sport + card + '-day-' + inp.value);
      if (d) { d.open = true; d.scrollIntoView({block: 'start'}); }
      else { inp.setCustomValidity('No card picks that day'); inp.reportValidity();
             inp.setCustomValidity(''); }
    });
  });
})();
</script>"""
