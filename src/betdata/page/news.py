"""The Research tab's "News alerts" section (owner decision 2026-11-05, MODEL.md "News
alerts"): the alerts switch (the M16 lag study's decision), the news alerts' own record (graded
picks, units, EV at close: separate from both cards), and the per-source table of early
claims (how often each source was first, by how much, and whether it was confirmed)."""

from __future__ import annotations

from html import escape
from pathlib import Path

from betdata import official as O
from betdata.page.official import stat_line

SIGNAL = "news-alert"


def news_html(rows: list[dict], site: Path, ledger: Path | None = None) -> str:
    from betdata.checks import picks_of, summary_line, three_checks
    from betdata.ledger import ledger_dir
    from betdata.news import ledger as NL

    led = ledger or ledger_dir()
    mine = [r for r in rows if r["signal_name"] == SIGNAL]
    rec = O.record(mine)
    href = "csv/signal-news-alert.csv"
    O.write_csv(site / href, rec.rows)
    ck = three_checks(picks_of(rec.rows))
    ev = ck.ev
    ev_txt = ("–" if ev.value is None else f"{100 * ev.value:+.1f}%"
              + ("" if ev.low is None else f" (95% {100 * ev.low:+.1f} to {100 * ev.high:+.1f})"))
    out = ["<section class='research' data-news='nba'><h2>News alerts</h2>"
           "<p class='meta'>Paper alerts, not card picks. The official NBA injury report is "
           "checked every minute in its windows; Bluesky insiders and verified team accounts and "
           "Sleeper trending adds count only when ahead of the report. A qualifying change "
           "reprices the game and teammates' props with Next Gen; an alert needs Next Gen's own "
           "bar.</p>",
           f"<p class='meta' data-switch>{escape(NL.switch_text(led))}</p>",
           f"<p>Record: {stat_line(rec, href)}</p>",
           f"<p class='meta'>EV at close: {escape(ev_txt)} · Three checks: "
           f"{escape(summary_line(ck))}</p>"]
    src = NL.source_table(NL.read(led))
    if not src:
        out.append("<p class='meta'>No early-source claims settled yet.</p>")
    else:
        body = "".join(
            f"<tr><td>{escape(r['account'] or r['source'])}</td><td>{r['first']}</td>"
            f"<td>{'–' if r['median_lead'] is None else r['median_lead']}</td>"
            f"<td>{r['confirmed']}</td><td>{r['contradicted']}</td><td>{r['unconfirmed']}</td>"
            f"<td>{r['official_first']}</td></tr>" for r in src)  # fmt: skip
        out.append("<table class='cmp news-sources'><thead><tr><th>Source</th><th>First</th>"
                   "<th>Median lead (min)</th><th>Confirmed</th><th>Contradicted</th>"
                   "<th>Unconfirmed</th><th>Official first</th></tr></thead>"
                   f"<tbody>{body}</tbody></table>")
    out.append("<p class='meta'><a href='csv/news-sources.csv'>Every settled claim (CSV)</a>"
               "</p></section>")
    src_csv = led / NL.FILE
    (site / "csv").mkdir(parents=True, exist_ok=True)
    (site / "csv/news-sources.csv").write_text(src_csv.read_text() if src_csv.exists()
                                                else ",".join(NL.FIELDS) + "\n")  # fmt: skip
    return "".join(out)
