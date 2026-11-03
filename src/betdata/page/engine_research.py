"""M20 Part G: the Research tab's compact "NBA engine" section (MODEL.md "M20: Part G").

Today's engine picks grouped by game (Player (TEAM) vs OPP), the signal's graded record (W-L-P
with units, graded picks only, flat 1 unit) and its three checks with their status. Research
only: the engine's picks are never on the card or the sport tabs."""

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
           "simulations a game), blended with the market. Research: never on the card.</p>"]
    if live is not None:
        n = len(live.get("games", []))
        out.append(f"<p class='meta'>Live predictions: {n} game{'s' if n != 1 else ''}, "
                   f"generated {escape(live.get('generated_at', '')[:16].replace('T', ' '))}"
                   " UTC.</p>")
    out.append(f"<p>Record: {stat_line(rec, href)}</p>")
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
