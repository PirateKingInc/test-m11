"""M20 Part G: the Research tab's compact "NBA engine" section (MODEL.md "M20: Part G").

Today's engine picks grouped by game (Player (TEAM) vs OPP), the signal's graded record (W-L-P
with units, graded picks only, flat 1 unit) and its three checks with their status. Research
only until 2026-11-04; since the owner override of that day the engine's picks are the NBA card's
pool (alerts/card.py): the ones chosen at 8:50 AM ET are official, the rest (and any found after
8:50) are tracked here."""

from __future__ import annotations

from datetime import date
from html import escape
from pathlib import Path

from betdata import official as O
from betdata.page.official import _result_cell, pick_line, stat_line

SIGNAL = "nba-engine"


def engine_html(rows: list[dict], today: date, site: Path, live: dict | None = None) -> str:
    from betdata.checks import picks_of, summary_line, three_checks

    mine = [r for r in rows if r["signal_name"] == SIGNAL]
    rec = O.record(mine)
    href = "csv/signal-nba-engine.csv"
    O.write_csv(site / href, rec.rows)
    ck = three_checks(picks_of(rec.rows))
    out = ["<section class='research' data-engine='nba'><h2>NBA engine</h2>"
           "<p class='meta'>One model from the players up (minutes, rates, team totals, 10,000 "
           "simulations a game), blended with the market. Since Nov 5 the NBA card is the top 5 "
           "engine picks still Live at 8:50 AM ET; every engine pick is listed here, card or "
           "not.</p>"]
    if live is not None:
        n = len(live.get("games", []))
        out.append(f"<p class='meta'>Live predictions: {n} game{'s' if n != 1 else ''}, "
                   f"generated {escape(live.get('generated_at', '')[:16].replace('T', ' '))}"
                   " UTC.</p>")
    out.append(f"<p>Record: {stat_line(rec, href)}</p>")
    out.append(predictions_html(site))
    out.append(f"<p class='meta' data-checks='{escape(ck.status)}'>Three checks: "
               f"{escape(summary_line(ck))}</p>")
    todays = [r for r in mine if O._et_date(r["tip"]) == today.isoformat()]
    if not todays:
        out.append("<p class='meta'>No engine picks today.</p>")
    games: dict[str, list[dict]] = {}
    for r in sorted(todays, key=lambda x: (x["tip"], x["game"])):
        games.setdefault(r["game"], []).append(r)
    for game, rs in games.items():
        out.append(f"<h3>{escape(game)}</h3><ul class='plist'>")
        for r in rs:
            out.append(f"<li class='prop'>{pick_line(r)}<div>{_result_cell(r)}</div></li>")
        out.append("</ul>")
    out.append("</section>")
    return "".join(out)


def predictions_html(site: Path, ledger: Path | None = None) -> str:
    """Every engine prediction graded at the close (owner override 2026-11-04): calibration and
    Brier against the closing no-vig line, overall, game markets and props."""
    from betdata.ledger import ledger_dir
    from betdata.nba import engine_grades as G

    rows = G.read(ledger or ledger_dir())
    href = "csv/nba-engine-predictions.csv"
    (site / href).parent.mkdir(parents=True, exist_ok=True)
    src = (ledger or ledger_dir()) / G.FILE
    (site / href).write_text(src.read_text() if src.exists() else ",".join(G.FIELDS) + "\n")
    out = ["<div class='engine-grades'><h3>Every prediction, graded at the close</h3>"
           "<p class='meta'>Each night, every game market and projected player prop with a "
           "stored DraftKings/FanDuel closing line: the engine's probability (its last pre-tip "
           "run) against the closing no-vig probability. Lower Brier is better; ECE is the "
           "calibration error over ten probability bins.</p><ul class='plist'>"]
    for label, kind in (("All", None), ("Game markets", "game"), ("Player props", "prop")):
        s = G.summary(rows, kind)
        out.append(f"<li class='prop' data-grades='{kind or 'all'}'><div><b>{label}</b></div>"
                   f"<div class='meta'>{escape(G.summary_line(s))}</div></li>")
    out.append(f"</ul><p class='meta'><a href='{href}'>Download every graded prediction (CSV)"
               "</a></p></div>")
    return "".join(out)
