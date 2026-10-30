"""The static page (GitHub Pages): one mobile-first index.html plus status.json, generated from
the database after each hourly run. Shared by all sports: tabs come from config/sports.yaml, the
games from each active sport's `cards`, the season record from its `records`.

Times are stored in UTC and shown in US Eastern. The warning banner is decided in the browser
from status.json (newest data more than 2 hours old, or the last run failed), so it appears
even if the runner stops running altogether.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path

from sqlalchemy import Connection, desc, select

from betdata.config import Settings
from betdata.db.schema import books, runner_runs
from betdata.moneyline import fair_american
from betdata.quotes import Quote
from betdata.runner.window import EASTERN
from betdata.sports import ACTIVE, Card, PropRow, RecordRow, Sport, SportEntry

STALE_HOURS = 2
REBUILT_NOTICE_HOURS = 48  # the rebuilt-from-scratch notice clears this long after
FOOTER = "Paper picks for research. Not betting advice."
POINT_LABELS = {
    "early": "early (before the final injury report)",
    "final_report": "final injury report",
    "prekick": "pre-kickoff (inactives known)",
    "none": "no prediction yet",
}
log = logging.getLogger(__name__)
MARKET_LABELS = {"points": "points", "rebounds": "rebounds", "assists": "assists",
                 "threes": "made threes", "pra": "pts+reb+ast", "pass_yds": "passing yds",
                 "rush_yds": "rushing yds", "rec_yds": "receiving yds",
                 "receptions": "receptions", "anytime_td": "anytime TD"}  # fmt: skip
SOURCE_TITLES = {
    "nflverse": "nflverse", "the_odds_api": "The Odds API", "sharpapi": "SharpAPI",
    "oddspapi": "OddsPapi", "cfbd": "CollegeFootballData", "open_meteo": "Open-Meteo",
    "espn_props": "ESPN",
}  # fmt: skip
STATUS_MARK = "<!--status-->"


def et(t: datetime | None, fmt: str = "%a %b %-d, %-I:%M %p") -> str:
    if t is None:
        return "-"
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(EASTERN).strftime(fmt) + " ET"


def american(x: int | None) -> str:
    return "-" if x is None else (f"+{x}" if x > 0 else str(x))


def spread_txt(team_home: str, team_away: str, home_line: float | None) -> str:
    """Home spread -3.5 -> 'HOME -3.5'; +3.5 -> 'AWAY -3.5'."""
    if home_line is None:
        return "-"
    if abs(home_line) < 0.05:
        return "pick'em"
    fav = team_home if home_line < 0 else team_away
    return f"{fav} -{abs(home_line):g}"


@dataclass
class RunInfo:
    last: dict | None  # the latest run (any status)
    last_ok: dict | None
    rebuilt_at: datetime | None
    today: list[dict]  # today's successful runs (Eastern date), oldest first


def _row(r) -> dict:
    return {"run_id": r.run_id, "status": r.status, "slot_at": r.slot_at,
            "started_at": r.started_at, "finished_at": r.finished_at or r.started_at,
            "message": r.message, "details": r.details or {}, "rebuilt": r.rebuilt}  # fmt: skip


def run_info(conn: Connection, now: datetime) -> RunInfo:
    rows = [_row(r) for r in conn.execute(
        select(runner_runs).where(runner_runs.c.started_at <= now)
        .order_by(desc(runner_runs.c.started_at)).limit(200))]  # fmt: skip
    finished = [r for r in rows if r["status"] != "running"]
    ok = [r for r in finished if r["status"] == "ok"]
    rebuilt = [r for r in ok if r["rebuilt"]]
    day = now.astimezone(EASTERN).date()
    today = [r for r in reversed(ok) if r["finished_at"].astimezone(EASTERN).date() == day]
    return RunInfo(finished[0] if finished else None, ok[0] if ok else None,
                   rebuilt[0]["finished_at"] if rebuilt else None, today)  # fmt: skip


def status_payload(info: RunInfo, now: datetime, missing_keys: list[str],
                   detected_keys: list[str] | None = None) -> dict:  # fmt: skip
    def iso(t):
        return None if t is None else t.astimezone(UTC).isoformat()

    last = info.last
    return {
        "generated_at": iso(now),
        "last_ok_at": iso(info.last_ok["finished_at"]) if info.last_ok else None,
        "last_run": None if last is None else {
            "status": last["status"], "at": iso(last["finished_at"]),
            "message": last["message"]},
        "rebuilt_at": iso(info.rebuilt_at) if info.rebuilt_at is not None
        and now - info.rebuilt_at < timedelta(hours=REBUILT_NOTICE_HOURS) else None,
        "stale_hours": STALE_HOURS,
        "missing_keys": missing_keys,
        "detected_keys": detected_keys or [],
    }  # fmt: skip


# --- rendering ------------------------------------------------------------------------------


def _quote_src(q: Quote | None, book_names: dict[str, str]) -> str:
    if q is None:
        return ""
    book = book_names.get(q.book, q.book)
    src = SOURCE_TITLES.get(q.source, q.source)
    name = book if book.lower().startswith(src.lower()) else f"{book} via {src}"
    return f"{name}, unchanged since {et(q.fetched_at, '%a %-I:%M %p')}"


def _edge(x: float | None, unit: str) -> str:
    return "-" if x is None else f"{x:+.1f} {unit}"


def card_html(c: Card, book_names: dict[str, str], movement: list[dict],
              ml_flags: bool = True, version_note: str | None = None) -> str:  # fmt: skip
    e = escape
    # M18 Part A: model-vs-market picks are research only, never flagged. A pick that clears
    # its threshold is listed in a muted note with the independent votes (agreement.py).
    picks, votes = [], ""
    if c.spread_pick:
        line = ""
        if c.market_spread is not None:
            home_line = c.market_spread.line
            line = f" {home_line if c.spread_pick == c.home else -home_line:+g}"
        picks.append(f"spread {e(c.spread_pick)}{line}")
        if c.spread_agree:
            votes = " · agrees: " + "; ".join(e(n) for n in c.spread_agree.notes)
        elif c.spread_agree is not None:
            votes = " · no Pinnacle or Kalshi vote"
    if c.ml_pick and ml_flags:
        q = c.ml_home if c.ml_pick == c.home else c.ml_away
        picks.append(f"moneyline {e(c.ml_pick)}{' ' + american(q.price) if q else ''}")
    agree_note = (f"<div class='agree no'>Model pick, research only (not flagged): "
                  f"{', '.join(picks)}{votes}</div>" if picks else "")
    bets: list[str] = []
    flag = "".join(f'<span class="flag">{b}</span>' for b in bets)
    changed = ("· model last changed " + e(et(c.predicted_at, "%a %-I:%M %p"))
               if c.predicted_at else "")  # fmt: skip
    rows = [_spread_row(c)]
    for team, model_p, quote, market_p, sign in (
        (c.home, c.model_win_home, c.ml_home, c.market_win_home, 1),
        (c.away, _flip(c.model_win_home), c.ml_away, _flip(c.market_win_home), -1),
    ):
        edge = None if c.ml_edge is None else sign * c.ml_edge
        rows.append(f"<tr><td>{e(team)} ML</td><td>{_price_pct(fair_american(model_p) if model_p
                    else None, model_p)}</td><td>{_price_pct(quote.price if quote else None,
                    market_p)}</td><td>{_edge(edge, 'pp')}</td></tr>")  # fmt: skip
    factors = "".join(f"<li>{e(lab)} <b>{v:+.1f}</b></li>" for lab, v in c.factors[:3])
    out_lines = "".join(
        f"<li><b>{e(team)}</b>: {e(', '.join(players))}</li>"
        for team, players in c.missing.items() if players
    )  # fmt: skip
    return f"""
<article class="game{' bet' if bets else ''}">
  <header><h3>{e(c.away)} @ {e(c.home)}</h3><time>{e(et(c.kickoff))}</time></header>
  {f'<div class="flags">{flag}</div>' if flag else ''}
  {agree_note}
  <table class="lines">
    <tr><th></th><th>Model</th><th>Market</th><th>Edge</th></tr>{"".join(rows)}
  </table>
  <p class="src">{_sources(c, book_names)}</p>
  <p class="meta">Prediction point: {e(POINT_LABELS.get(c.point, c.point))} {changed}</p>
  {f'<div class="why"><h4>Top factors (points for {e(c.home)})</h4><ul>{factors}</ul></div>'
   if factors else ''}
  {f'<div class="out"><h4>Likely out</h4><ul>{out_lines}</ul></div>' if out_lines else ''}
  {f'<p class="meta">{e(" · ".join(c.notes))}</p>' if c.notes else ''}
  {movement_html(c, movement)}{f"""
  <p class="meta ver">{e(version_note)}</p>""" if version_note else ''}
</article>"""


def _flip(p: float | None) -> float | None:
    return None if p is None else 1 - p


def _price_pct(price: int | None, p: float | None) -> str:
    if price is None:
        return "-"
    return american(price) + ("" if p is None else f" <small>({100 * p:.0f}%)</small>")


def _spread_row(c: Card) -> str:
    """Model and market spread, and the edge as the team the model likes and by how much."""
    mkt = c.market_spread.line if c.market_spread else None
    edge = "-"
    if c.spread_edge is not None:
        team = c.home if c.spread_edge > 0 else c.away
        edge = f"{escape(team)} +{abs(c.spread_edge):.1f}"
    return (f"<tr><td>Spread</td><td>{escape(spread_txt(c.home, c.away, c.model_spread_home))}"
            f"</td><td>{escape(spread_txt(c.home, c.away, mkt))}</td><td>{edge}</td></tr>")


def _sources(c: Card, book_names: dict[str, str]) -> str:
    s, m = _quote_src(c.market_spread, book_names), _quote_src(c.ml_home, book_names)
    if not s and not m:
        return "No market lines yet."
    if s == m:
        return "Market: " + escape(s)
    return (f"Spread: {escape(s) or 'no market line'}<br>"
            f"Moneyline: {escape(m) or 'no market moneyline'}")


def movement_html(c: Card, points: list[dict]) -> str:
    if len(points) < 2:
        return ""
    rows = "".join(
        f"<tr><td>{e_t(p['at'])}</td><td>{spread_txt(c.home, c.away, p['model'])}</td>"
        f"<td>{spread_txt(c.home, c.away, p['market'])}</td>"
        f"<td>{'-' if p['win'] is None else f'{100 * p['win']:.0f}%'}</td>"
        f"<td>{'-' if p['mwin'] is None else f'{100 * p['mwin']:.0f}%'}</td></tr>"
        for p in points
    )  # fmt: skip
    return f"""<details class="move"><summary>Line movement today ({len(points)} runs)</summary>
  <table><tr><th>Run</th><th>Model</th><th>Market</th><th>Win</th><th>Mkt win</th></tr>
  {rows}</table></details>"""


def e_t(t: datetime) -> str:
    return et(t, "%-I:%M %p")


TOO_FEW = 100  # under this many picks a record is shown, but not judged


def _rec_cells(r: RecordRow) -> str:
    roi = "-" if r.roi is None else f"{100 * r.roi:+.1f}%"
    if r.bets < TOO_FEW:
        roi += " <span class='few'>too few to judge</span>"
    clv = "-" if r.avg_clv is None else f"{r.avg_clv:+.2f} {r.clv_unit}"
    beat = "-" if r.beat_close is None else f"{100 * r.beat_close:.0f}%"
    if r.ties:
        beat += f" <span class='few'>{r.ties} at the close</span>"
    return (f"<td>{escape(r.market.title())}</td><td>{r.wins}-{r.losses}-{r.pushes}</td>"
            f"<td>{roi}</td><td>{clv}</td><td>{beat}</td><td>{r.bets}</td>")


REC_HEAD = ("<tr><th>Market</th><th>W-L-P</th><th>ROI</th><th>Avg CLV</th>"
            "<th>Beat close</th><th>Picks</th></tr>")


def _is_version(model_version: str, page_version: str | None) -> bool:
    if not page_version:
        return False
    if ":" in page_version:
        return model_version == page_version
    return model_version.split(":")[0] == page_version


def records_html(rows: list[RecordRow], page_version: str | None = None,
                 markets: tuple[str, ...] = ("spread", "spread agreement", "moneyline"),
                 engine_version: str | None = None) -> str:  # fmt: skip
    """The season record of the version the page shows: one row per market, spread and
    moneyline, under one counting rule; every other version and market in a collapsed
    section of background comparisons (M15.1)."""
    from betdata.nfl.grade import RECORD_RULE

    main = {r.market: r for r in rows if _is_version(r.model_version, page_version)
            and r.market in markets}  # fmt: skip
    rule = (f"<p class='meta'>What is counted: {escape(RECORD_RULE)} Beating the close: share "
            "of picks that got a better number than the closing line, among picks whose number "
            "differed from it. W-L-P: wins, losses, pushes. Under "
            f"{TOO_FEW} picks a record is too few to judge.</p>")
    if main:
        body = "".join(f"<tr>{_rec_cells(main[m])}</tr>" for m in markets if m in main)
        table = (f"<p class='meta'>Model version {escape(page_version or '')}</p>"
                 f"<div class='recwrap'><table class='rec'>{REC_HEAD}{body}</table>"
                 "</div>")  # fmt: skip
    else:
        table = "<p class='meta'>No graded picks for this model version yet.</p>"
    eng = ""
    if engine_version:
        er = [r for r in rows if _is_version(r.model_version, engine_version)
              and r.market == "spread"]  # fmt: skip
        body = "".join(f"<tr>{_rec_cells(r)}</tr>" for r in er)
        eng = (f"<p class='meta'>Engine (market-free) {escape(er[0].model_version)}, spread: "
               "same counting rule</p><div class='recwrap'><table class='rec'>"
               f"{REC_HEAD}{body}</table></div>" if er else
               "<p class='meta'>Engine (market-free): no graded spread picks yet.</p>")
    others = [r for r in rows if not (_is_version(r.model_version, page_version)
                                      and r.market in markets)
              and not (engine_version and _is_version(r.model_version, engine_version))]
    other = ""
    if others:
        body = "".join(f"<tr>{_rec_cells(r)}<td class='ver'>{escape(r.model_version)}</td></tr>"
                       for r in others)  # fmt: skip
        other = ("<details class='other'><summary>Other model versions (background "
                 "comparisons)</summary><p class='meta'>Graded live by the same rule but not "
                 "shown on the cards; for comparison only.</p><div class='recwrap'>"
                 "<table class='rec'>"
                 f"{REC_HEAD.replace('</tr>', '<th>Model</th></tr>')}{body}</table></div>"
                 "</details>")  # fmt: skip
    if not rows:
        return rule + "<p class='meta'>No graded picks yet.</p>" + eng
    return rule + table + eng + other


def jobs_html(last: dict | None, sport: str) -> str:
    if not last:
        return ""
    jobs = [j for j in last["details"].get("jobs", []) if j.get("sport") == sport]
    if not jobs:
        return ""
    items = "".join(
        f"<li>{escape(SOURCE_TITLES.get(j['source'], j['source']))} {escape(j['job'])}: "
        f"{escape(j['status'])}{(' — ' + escape(j['message'])) if j.get('message') else ''}</li>"
        for j in jobs
    )  # fmt: skip
    return (f"<details class='jobs'><summary>Data refresh, last run</summary><ul>{items}</ul>"
            "</details>")


def version_note(sport: Sport) -> str | None:
    v = sport.page_version()
    if not v:
        return None
    bg = sport.background_versions()
    tail = (f" · also running in the background (graded live, not shown): {', '.join(bg)}"
            if bg else "")  # fmt: skip
    return f"Model version: {v}{tail}"


def _price(x: int | None) -> str:
    return "–" if x is None else american(x)


def props_html(rows: list[PropRow], book_names: dict[str, str]) -> str:
    """Per game, each player prop with a stored line: projection, line and prices, P(over),
    edge, top 2 factors, and the line's source, book and fetch time."""
    if not rows:
        return ("<section class='props'><h2>Props</h2><p class='meta'>No player prop lines "
                "stored for upcoming games yet.</p></section>")  # fmt: skip
    e = escape
    out, by_game = [], {}
    for r in rows:
        by_game.setdefault(r.game_id, []).append(r)
    for rs in by_game.values():
        g = rs[0]
        items = []
        for r in rs:
            fac = "; ".join(f"{lab} {v:+.1f} {u}" for lab, v, u in
                            [f for f in (r.factors or []) if abs(f[1]) >= 0.05][:2])
            book = book_names.get(r.book, r.book) if r.book else "?"
            src = SOURCE_TITLES.get(r.source, r.source) if r.source else "?"
            # M18 Part A: prop model picks are research only, never flagged
            badge = (f'<span class="research">model leans {e(r.flag)}, research only</span>'
                     if r.flag else "")  # fmt: skip
            research = ('<span class="research">research only, not backtested</span>'
                        if r.research else
                        '<span class="research">info only (backtest ROI range includes 0)'
                        "</span>" if r.info else "")  # fmt: skip
            line = "–" if r.line is None else f"{r.line:g}"
            # M18 Part D: the median of the model's distribution, the mean beside it
            proj = (f"Median <b>{r.median:g}</b> (mean {r.mean:.1f})" if r.median is not None
                    else f"Proj <b>{r.mean:.1f}</b>")  # fmt: skip
            p = "–" if r.p_over is None else f"{100 * r.p_over:.0f}%"
            edge = "–" if r.edge is None else f"{r.edge:+.1f} pp"
            items.append(
                f"<li class='prop'><div><b>{e(r.player)}</b> · "
                f"{e(MARKET_LABELS.get(r.market, r.market))} {badge}{research}</div>"
                f"<div>{proj} · line {line} (o {_price(r.over_price)} / "
                f"u {_price(r.under_price)}) · P(over) {p} · edge {edge}</div>"
                + (f"<div class='meta'>{e(fac)}</div>" if fac else "")
                + f"<div class='src'>{e(book)} via {e(src)}, "
                f"{e(et(r.fetched_at, '%a %-I:%M %p')) if r.fetched_at else '?'}</div></li>")
        out.append(f"<article class='game'><header><h3>{e(g.away)} @ {e(g.home)}</h3>"
                   f"<time>{e(et(g.kickoff))}</time></header><ul class='plist'>"
                   f"{''.join(items)}</ul></article>")  # fmt: skip
    return ("<section class='props'><h2>Props</h2><p class='meta'>Players with a stored prop "
            "line. Edge = model P(over) minus the no-vig over probability. Research only: "
            "model picks are never flagged (M18).</p>" + "".join(out) + "</section>")


def sport_panel(sport: Sport, cards: list[Card], movement: dict, records: list[RecordRow],
                book_names: dict, info: RunInfo, props: list[PropRow] | None = None,
                engine: str | None = None, value: str | None = None) -> str:  # fmt: skip
    roi = sport.moneyline_final_roi()
    ml_flags = roi is not None and roi > 0
    note = version_note(sport)
    games = "".join(card_html(c, book_names, movement.get(c.game_id, []), ml_flags, note)
                    for c in cards)  # fmt: skip
    if not games:
        games = "<p class='meta'>No games in the next 8 days.</p>"
    legend_items = dict(sport.thresholds())
    if "moneyline" in legend_items and not ml_flags:
        legend_items["moneyline"] = (
            "edges shown for information only, never flagged: moneyline flags need a positive "
            "final-test ROI" + ("" if roi is None else f" (it was {100 * roi:+.1f}%)"))
    legend = "".join(f"<li>{escape(k)}: {escape(v)}</li>" for k, v in legend_items.items())
    research = ("<h2 class='research-head'>Model picks (research only)</h2><p class='meta'>"
                "The models below are research: no pick is flagged (M18). Value is above."
                "</p>") if value else ""  # fmt: skip
    return f"""
{value or ""}
{research}
<section class="games">{games}</section>
{engine or ""}
{props_html(props or [], book_names) if props is not None else ""}
<section class="legend"><h2>Model picks (research only)</h2><p>Since M18 no model-vs-market
  pick is flagged. A pick is listed and graded, for research, when it clears the threshold
  chosen on validation seasons:</p><ul>{legend}</ul></section>
<section><h2>Season record</h2>{records_html(records, sport.page_version(),
                                             engine_version=sport.engine_version())}</section>
{jobs_html(info.last, sport.key)}"""


def movement_for(conn: Connection, sport: Sport, info: RunInfo) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for run in info.today:
        for c in sport.cards(conn, run["finished_at"]):
            out.setdefault(c.game_id, []).append({
                "at": run["finished_at"], "model": c.model_spread_home,
                "market": c.market_spread.line if c.market_spread else None,
                "win": c.model_win_home, "mwin": c.market_win_home})  # fmt: skip
    return out


def detected_keys(settings: Settings) -> list[str]:
    """Keyed sources whose key this run found (the secret's name, never its value)."""
    return sorted(
        f"{SOURCE_TITLES.get(name, name)} ({cfg.api_key_env})"
        for name, cfg in settings.sources.items()
        if cfg.enabled and cfg.api_key_env and cfg.api_key
    )  # fmt: skip


def missing_keys(settings: Settings) -> list[str]:
    return sorted(
        f"{SOURCE_TITLES.get(name, name)} ({cfg.api_key_env})"
        for name, cfg in settings.sources.items()
        if cfg.enabled and cfg.api_key_env and not cfg.api_key
    )  # fmt: skip


def render(entries: list[SportEntry], panels: dict[str, str], status: dict) -> str:
    tabs, sections = [], []
    for i, en in enumerate(entries):
        active = en.status == ACTIVE
        soon = "" if active else " <small>coming soon</small>"
        tabs.append(f'<button role="tab" data-tab="{escape(en.key)}"'
                    f'{" aria-selected=true" if i == 0 else ""}>{escape(en.name)}{soon}</button>')
        body = panels.get(en.key) or (f"<p class='soon'>{escape(en.name)} predictions are coming "
                                      "soon.</p>")  # fmt: skip
        sections.append(f'<div class="panel" id="tab-{escape(en.key)}"'
                        f'{"" if i == 0 else " hidden"}>{body}</div>')
    keys, found = status.get("missing_keys") or [], status.get("detected_keys") or []
    keys_note = ("<p>API keys detected in this run: " + escape(", ".join(found) or "none")
                 + ". " + ("Sources skipped because their key is not set: "
                           + escape(", ".join(keys)) + "." if keys else "No key is missing.")
                 + "</p>")  # fmt: skip
    payload = json.dumps(status).replace("</", "<\\/")
    return PAGE.format(
        updated=escape(et(datetime.fromisoformat(status["last_ok_at"]))
                       if status.get("last_ok_at") else "never"),
        tabs="".join(tabs), panels="".join(sections), footer=escape(FOOTER), keys=keys_note,
        status=f'{STATUS_MARK}<script id="status" type="application/json">{payload}</script>',
    )  # fmt: skip


def build_site(conn: Connection, settings: Settings, entries: list[SportEntry],
               active: list[Sport], out_dir: Path, now: datetime) -> dict:  # fmt: skip
    info = run_info(conn, now)
    book_names = {k: n for k, n in conn.execute(select(books.c.key, books.c.name))}
    panels, engines, values = {}, {}, {}
    from betdata.nfl.book_lines import poller_conn

    poller = poller_conn()
    record = _ledger(out_dir)
    for sport in active:
        eng = _engine(sport, conn, now)
        if eng is not None:  # the run log's proof that the section was published (M17)
            engines[sport.key] = eng.count("<article class='game'>")
        val = _value(sport, poller, now, record)
        if val is not None:  # the run log's proof that the Value section was published (M18)
            values[sport.key] = val.count("<li class='prop'>")
        panels[sport.key] = sport_panel(sport, sport.cards(conn, now),
                                        movement_for(conn, sport, info), sport.records(conn),
                                        book_names, info, _props(sport, conn, now),
                                        eng, val)  # fmt: skip
    if poller is not None:
        poller.close()
    status = status_payload(info, now, missing_keys(settings), detected_keys(settings))
    if engines:
        status["engine_sections"] = engines
    status["value_sections"] = values
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render(entries, panels, status))
    (out_dir / "status.json").write_text(json.dumps(status, indent=1))
    (out_dir / ".nojekyll").write_text("")
    return status


def _ledger(out_dir: Path) -> str:
    """The ledger's all-time record (HTML), and site/picks.csv: every pick with its result
    fields, for the page's download link. A failure only drops them."""
    try:
        import csv

        from betdata.ledger import PICK_FIELDS, RESULT_FIELDS, joined, ledger_dir
        from betdata.page.value import record_html

        rows = joined(ledger_dir())
        out_dir.mkdir(parents=True, exist_ok=True)
        with (out_dir / "picks.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[*PICK_FIELDS, *RESULT_FIELDS[1:]],
                               lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        return record_html(rows)
    except Exception as e:  # never lose the page over the ledger
        log.warning("ledger record failed: %s", e)
        return ""


def _value(sport: Sport, poller, now: datetime, record: str = "") -> str | None:
    """The sport's Value section (M18) with the ledger's all-time record; a failure only drops
    it."""
    if sport.key not in ("nba", "nfl"):
        return None
    try:
        from betdata.page.value import value_html

        return value_html(sport.key, poller, now, et, record)
    except Exception as e:  # never lose the page over the Value section
        log.warning("%s value section failed: %s", sport.key, e)
        return None


def _engine(sport: Sport, conn: Connection, now: datetime) -> str | None:
    """The sport's extra engine section (M17); a failure only drops it."""
    try:
        return sport.engine_html(conn, now, et)
    except Exception as e:  # never lose the page over the engine section
        log.warning("%s engine section skipped: %s", sport.key, e)
        return None


def _props(sport: Sport, conn: Connection, now: datetime) -> list[PropRow] | None:
    """The sport's props section (None: the sport has no props); a failure only drops it."""
    if type(sport).props is Sport.props:
        return None
    try:
        return sport.props(conn, now)
    except Exception as e:  # never lose the page over props
        log.warning("%s props section skipped: %s", sport.key, e)
        return []


def mark_failed(site: Path, last_good: Path | None, now: datetime, message: str,
                run_url: str | None = None) -> None:  # fmt: skip
    """After a failed run: put the last good page back with the failure recorded in its status
    (the page shows the warning); if there is no good page yet, a minimal one."""
    if last_good is not None and (last_good / "index.html").exists():
        if site.exists():
            shutil.rmtree(site)
        shutil.copytree(last_good, site)
        status = json.loads((site / "status.json").read_text())
    else:
        site.mkdir(parents=True, exist_ok=True)
        status = {"generated_at": None, "last_ok_at": None, "rebuilt_at": None,
                  "stale_hours": STALE_HOURS, "missing_keys": []}  # fmt: skip
        (site / "index.html").write_text(render([], {}, status))
    status["last_run"] = {"status": "failed", "at": now.astimezone(UTC).isoformat(),
                          "message": message, "url": run_url}  # fmt: skip
    (site / "status.json").write_text(json.dumps(status, indent=1))
    html = (site / "index.html").read_text()
    head, _, rest = html.partition(STATUS_MARK)
    tail = rest.split("</script>", 1)[1] if rest else ""
    payload = json.dumps(status).replace("</", "<\\/")
    (site / "index.html").write_text(
        f'{head}{STATUS_MARK}<script id="status" type="application/json">{payload}</script>{tail}')
    (site / ".nojekyll").write_text("")


def stale(status: dict, now: datetime) -> bool:
    ok = status.get("last_ok_at")
    return ok is None or now - datetime.fromisoformat(ok) > timedelta(hours=STALE_HOURS)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>betdata lines</title>
<style>
:root{{--bg:#fff;--fg:#111;--mut:#666;--line:#ddd;--card:#f7f7f8;--acc:#0b6;--warn:#b00;
--wbg:#fde8e8;--flag:#e6f7ee}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--mut:#9a9a9a;--line:#333;
--card:#1b1b1d;--acc:#3d9;--warn:#f77;--wbg:#3a1616;--flag:#143322}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.4 -apple-system,system-ui,
"Segoe UI",Roboto,sans-serif}}
main,header.top,footer{{max-width:720px;margin:0 auto;padding:0 16px}}
header.top{{padding-top:12px}}
h1{{font-size:1.25rem;margin:0}} h2{{font-size:1.1rem;margin:20px 0 8px}}
h3{{font-size:1.05rem;margin:0}} h4{{font-size:.9rem;margin:8px 0 2px}}
.updated{{color:var(--mut);font-size:.9rem;margin:2px 0 8px}}
#warn{{background:var(--wbg);color:var(--warn);border-radius:8px;padding:8px 12px;margin:8px 0;
font-weight:600}}
nav{{display:flex;gap:8px;margin:8px 0;position:sticky;top:0;background:var(--bg);padding:6px 0;
z-index:1}}
nav button{{flex:1;font:inherit;padding:10px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--fg)}}
nav button[aria-selected=true]{{border-color:var(--acc);font-weight:700}}
nav small{{color:var(--mut);font-weight:400}}
.game{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;
margin:10px 0}}
.game.bet{{border-color:var(--acc)}}
.game>header{{display:flex;justify-content:space-between;align-items:baseline;gap:8px;
flex-wrap:wrap}}
.game time{{color:var(--mut);font-size:.9rem}}
.flags{{margin:6px 0}} .agree{{font-size:.78rem;color:var(--acc);margin:2px 0 6px}}
.agree.no{{color:var(--mut)}} .value{{border:2px solid var(--acc);border-radius:10px;
padding:8px 12px;margin:8px 0 14px}} .research-head{{color:var(--mut)}}
.tablewrap{{overflow-x:auto}} .record td,.record th{{font-size:.8rem;white-space:nowrap}}
.record tr.grp th{{text-align:left;padding-top:8px}}
.flag{{display:inline-block;background:var(--flag);color:var(--acc);
border-radius:6px;padding:2px 8px;margin:2px 4px 2px 0;font-size:.85rem;font-weight:700}}
table{{width:100%;border-collapse:collapse;font-size:.9rem}}
th,td{{text-align:left;padding:4px 4px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{color:var(--mut);font-weight:600}}
.src,.meta{{color:var(--mut);font-size:.8rem;margin:6px 0 0}}
ul{{margin:2px 0;padding-left:18px;font-size:.9rem}}
details{{margin-top:8px;font-size:.9rem}} summary{{cursor:pointer;color:var(--acc)}}
.rec .ver{{font-size:.7rem;color:var(--mut);word-break:break-all}}
.recwrap{{overflow-x:auto;max-width:100%}}
table.rec{{font-size:.8rem}} .rec th,.rec td{{padding:4px 3px}}
.rec td:nth-child(2){{white-space:nowrap}}
.rec .few{{display:block;font-size:.68rem;color:var(--mut)}}
.soon{{padding:24px 0;color:var(--mut)}}
.plist{{list-style:none;padding:0;margin:6px 0 0}} .prop{{padding:6px 0;
border-bottom:1px solid var(--line);font-size:.9rem}} .prop:last-child{{border-bottom:0}}
.research{{display:inline-block;color:var(--mut);border:1px solid var(--line);border-radius:6px;
padding:0 6px;margin-left:4px;font-size:.75rem}}
footer{{color:var(--mut);font-size:.8rem;padding:24px 16px 32px}}
</style></head>
<body>
<header class="top"><h1>betdata · lines</h1>
<p class="updated">Last updated: <span id="updated">{updated}</span></p>
<div id="warn" hidden></div></header>
<main><nav role="tablist">{tabs}</nav>{panels}</main>
<footer><p><b>{footer}</b></p>{keys}
<p>Weather data by <a href="https://open-meteo.com/">Open-Meteo.com</a> (CC BY 4.0), used
non-commercially.</p>
<p>NFL data: <a href="https://github.com/nflverse">nflverse</a> (CC BY 4.0); charting data:
FTN Data via nflverse (CC BY-SA 4.0). NBA data: ESPN via sportsdataverse; injury reports: NBA.
Prop lines: ESPN.</p>
<p>Lines are paper picks from the M12 model; markets are the latest line each source had when
the page was built. Spreads are listed with the favourite. Edges are from the home side's view.
</p></footer>
{status}
<script>
(function(){{
  var tabs=document.querySelectorAll('nav button');
  tabs.forEach(function(b){{b.addEventListener('click',function(){{
    tabs.forEach(function(x){{x.setAttribute('aria-selected',x===b);
      document.getElementById('tab-'+x.dataset.tab).hidden=(x!==b);}});
  }});}});
  function fmt(iso){{return new Date(iso).toLocaleString('en-US',{{timeZone:'America/New_York',
    weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}})+' ET';}}
  function show(s){{
    var w=document.getElementById('warn'),msg=[];
    if(s.last_ok_at){{
      document.getElementById('updated').textContent=fmt(s.last_ok_at);
      var age=(Date.now()-new Date(s.last_ok_at))/3600000;
      if(age>s.stale_hours)msg.push('Data is '+Math.floor(age)+' hours old (updates run hourly '+
        '7am-10pm ET).');
    }} else msg.push('No successful update yet.');
    if(s.last_run&&s.last_run.status==='failed')msg.push('The last update failed ('+
      fmt(s.last_run.at)+'): '+(s.last_run.message||'error')+'. Showing the last good data.');
    if(s.rebuilt_at&&(Date.now()-new Date(s.rebuilt_at))<{rebuilt_hours}*36e5)msg.push(
      'Saved data was lost and rebuilt from free sources on '+fmt(s.rebuilt_at)+
      '; predictions and graded picks from before then are gone.');
    w.hidden=!msg.length; w.textContent=msg.join(' ');
  }}
  var s=JSON.parse(document.getElementById('status').textContent); show(s);
  fetch('status.json',{{cache:'no-store'}}).then(function(r){{return r.json();}})
    .then(show).catch(function(){{}});
}})();
</script>
</body></html>
""".replace("{rebuilt_hours}", str(REBUILT_NOTICE_HOURS))
