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
    settled = NL.read(led)
    # claims: Bluesky posts only; Sleeper add spikes are hints, on their own line (2026-11-09)
    src = NL.source_table([r for r in settled if r["source"] != "sleeper"])
    hints = [r for r in settled if r["source"] == "sleeper"]
    corrob = sum(r.get("corroborated") == "true" for r in settled if r["source"] != "sleeper")
    if not src:
        out.append("<p class='meta'>No early-source claims settled yet.</p>")
    else:
        out.append("<ul class='plist news-sources'>" + "".join(
            f"<li class='prop' data-source='{escape(r['account'] or r['source'])}'><div><b>"
            f"{escape(r['account'] or r['source'])}</b></div><div class='meta'>First "
            f"{r['first']} · median lead "
            f"{'–' if r['median_lead'] is None else str(r['median_lead']) + ' min'}</div>"
            f"<div class='meta'>Confirmed {r['confirmed']} · contradicted {r['contradicted']} "
            f"· unconfirmed {r['unconfirmed']}</div><div class='meta'>Official first "
            f"{r['official_first']}</div></li>" for r in src) + "</ul>")  # fmt: skip
    out.append(f"<p class='meta' data-hints>Sleeper add spikes: {len(hints)} logged, {corrob} "
               "corroborated a claim (hints only: never alerts, not counted as claims).</p>")
    out.append("<p class='meta'><a href='csv/news-sources.csv'>Every settled claim (CSV)</a>"
               "</p></section>")
    src_csv = led / NL.FILE
    (site / "csv").mkdir(parents=True, exist_ok=True)
    (site / "csv/news-sources.csv").write_text(src_csv.read_text() if src_csv.exists()
                                                else ",".join(NL.FIELDS) + "\n")  # fmt: skip
    return "".join(out)


def filters_html(rows: list[dict]) -> str:
    """The Research tab's "Filters" section (rules v2, owner 2026-11-09)."""
    from betdata import eligibility as EL

    t = EL.filters_table(rows)
    out = ["<section class='research' data-filters='nba'><h2>Filters (rules v2)</h2>"
           f"<p class='meta'>Since {EL.RULES_V2_START:%b %-d}: what each card-eligibility rule "
           "removed (still created and graded, flat 1 unit), against the card's own record over "
           "the same period. Provisional rules, chosen after seeing the data to Nov 9.</p>"]
    if not t:
        out.append("<p class='meta'>No removed pick yet.</p>")
    else:
        out.append("<ul class='plist filters'>" + "".join(
            f"<li class='prop' data-reason='{escape(r['reason'])}'><div><b>{escape(r['card'])}"
            f" · {escape(r['reason'])}</b></div><div class='meta'>Removed {r['removed']} · "
            f"graded {r['wlp']}, {r['units']:+.2f}u · win {EL.pct(r['win'])} vs needed "
            f"{EL.pct(r['needed'])}</div><div class='meta'>Card same period: {r['card_wlp']}, "
            f"{r['card_units']:+.2f}u ({r['card_graded']} graded) · {escape(r['status'])}</div>"
            "</li>" for r in t) + "</ul>")  # fmt: skip
    out.append(f"<p class='meta'>{escape(EL.REVIEW_RULE)}</p></section>")
    return "".join(out)
