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
    goal blocks. The page's fold script remembers a reader who opens it.

    NBA (owner override 2026-11-04): the engine card is the headline (its periods and its own
    goal, restarted on Nov 5), Card v1 (nbap15, Oct 31 – Nov 4) is a folded line below it, and
    the "All sports" goal combines both versions and the NFL card, labelled so."""
    off = O.official(rows, sport)
    nba = sport == "nba"
    eng_head = O.nba_headline() == "engine"  # "v1" after the one-step revert (SWITCH.md)
    head = (O.engine_card(off) if eng_head else O.card_v1(off)) if nba else off
    per = O.periods(head, today)
    allt, g = per["All time"], O.goal(O.goal_rows(rows, sport))
    who = ("Engine card" if eng_head else "Card v1") if nba else "All time"
    preview = (f"{who} {allt.wlp} · {_units(allt.units)} · "
               f"goal {g.graded} of {O.GOAL_N}, {g.status}")
    out = [f"<section class='official' data-sport='{escape(sport)}'>"
           f"<details class='fold' data-fold='{escape(sport)}-official'><summary><b>Official "
           f"record · {escape(name)}</b> <span class='pv'>— {escape(preview)}</span></summary>"
           "<div class='foldbody'><p class='meta'>Official picks are the daily card (up to 5, "
           "chosen at 8:50 AM ET). Like a team's record, graded picks only; today's picks are "
           "under Tonight's card until they are graded. Paper units, not advice.</p>"]
    if nba:
        out.append(f"<h3 class='era' data-era='{'engine' if eng_head else 'v1'}'>"
                   f"{escape(O.ENGINE_CARD if eng_head else O.CARD_V1_REVERTED)}</h3>")
    out.append("<dl class='periods'>")
    for label, rec in per.items():
        slug = label.lower().replace(" ", "-")
        href = f"csv/{sport}-official-{slug}.csv"
        O.write_csv(site / href, rec.rows)
        out.append(f"<dt>{escape(label)}</dt><dd>{stat_line(rec, href)}</dd>")
    out.append("</dl>")
    out.append(goal_html(O.goal_rows(rows, sport),
                         ("NBA engine card" if eng_head else "NBA Card v1") if nba else name))
    if nba:
        v1 = O.record(O.card_v1(off) if eng_head else O.engine_card(off), O.official_stake)
        href = f"csv/nba-official-{'card-v1' if eng_head else 'engine-card'}.csv"
        O.write_csv(site / href, v1.rows)
        title = O.CARD_V1 if eng_head else O.ENGINE_CARD_REVERTED
        out.append(f"<details class='fold era' data-fold='nba-card-v1' data-era='v1'><summary>"
                   f"<b>{escape(title)}</b> <span class='pv'>— {v1.wlp} · "
                   f"{_units(v1.units)}</span></summary><div class='foldbody'>"
                   f"<p>{stat_line(v1, href)}</p></div></details>")
        # "All sports" once, on the NBA tab (as the M18 bankroll was): both NBA versions + NFL
        out.append(goal_html(O.official(rows), O.ALL_SPORTS, O.ALL_SPORTS_NOTE))
    out.append("</div></details></section>")
    return "".join(out)


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
    off = O.official(rows, sport)
    days = O.by_day(off)
    week = [r for r in off if r["day"] >= (today - timedelta(days=6)).isoformat()]
    out = [f"<section class='days' data-sport='{escape(sport)}'><h2>Results by day</h2>"
           f"<p class='meta'>Last 7 days, why picks won or lost: {_tags(week)}</p>"
           f"<label class='jump'>Jump to a day <input type='date' class='dayjump' "
           f"min='{escape(min(days) if days else '')}' max='{escape(today.isoformat())}'>"
           "</label>"]
    if not days:
        out.append("<p class='meta'>No card picks yet.</p>")
    for d, rs in days.items():
        rec = O.record(rs, O.official_stake)
        href = f"csv/{sport}-day-{d}.csv"
        O.write_csv(site / href, [r for r in rows if r["sport"] == sport and r["day"] == d
                                  and r.get("result")])  # graded and void, never pending
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
                            + "</div></li>" for r in ps) + "</ul></div>")  # fmt: skip
        out.append(f"<details class='day' id='{escape(sport)}-day-{escape(d)}' "
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
            retired = (f" <span class='meta archived'>archived from the card "
                       f"{escape(ARCHIVED[name])}: shadow Card v1 only, never pushed; history "
                       "kept</span>")
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
      var sport = inp.closest('section').dataset.sport;
      var d = document.getElementById(sport + '-day-' + inp.value);
      if (d) { d.open = true; d.scrollIntoView({block: 'start'}); }
      else { inp.setCustomValidity('No card picks that day'); inp.reportValidity();
             inp.setCustomValidity(''); }
    });
  });
})();
</script>"""
