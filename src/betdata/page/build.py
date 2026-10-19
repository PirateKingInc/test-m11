"""The static page (GitHub Pages): one mobile-first index.html plus status.json, generated from
the database after each hourly run. Shared by all sports: tabs come from config/sports.yaml, the
games from each active sport's `cards`, the season record from its `records`.

Times are stored in UTC and shown in US Eastern. The warning banner is decided in the browser
from status.json (newest data more than 2 hours old, or the last run failed), so it appears
even if the runner stops running altogether.
"""

from __future__ import annotations

import json
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
from betdata.sports import ACTIVE, Card, RecordRow, Sport, SportEntry

STALE_HOURS = 2
REBUILT_NOTICE_DAYS = 7
FOOTER = "Paper picks for research. Not betting advice."
POINT_LABELS = {
    "early": "early (before the final injury report)",
    "final_report": "final injury report",
    "prekick": "pre-kickoff (inactives known)",
    "none": "no prediction yet",
}
SOURCE_TITLES = {
    "nflverse": "nflverse", "the_odds_api": "The Odds API", "sharpapi": "SharpAPI",
    "oddspapi": "OddsPapi", "cfbd": "CollegeFootballData", "open_meteo": "Open-Meteo",
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


def status_payload(info: RunInfo, now: datetime, missing_keys: list[str]) -> dict:
    def iso(t):
        return None if t is None else t.astimezone(UTC).isoformat()

    last = info.last
    return {
        "generated_at": iso(now),
        "last_ok_at": iso(info.last_ok["finished_at"]) if info.last_ok else None,
        "last_run": None if last is None else {
            "status": last["status"], "at": iso(last["finished_at"]),
            "message": last["message"]},
        "rebuilt_at": iso(info.rebuilt_at),
        "stale_hours": STALE_HOURS,
        "missing_keys": missing_keys,
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


def card_html(c: Card, book_names: dict[str, str], movement: list[dict]) -> str:
    e = escape
    bets = []
    if c.spread_pick:
        line = ""
        if c.market_spread is not None:
            home_line = c.market_spread.line
            line = f" {home_line if c.spread_pick == c.home else -home_line:+g}"
        bets.append(f"Spread: {e(c.spread_pick)}{line}")
    if c.ml_pick:
        q = c.ml_home if c.ml_pick == c.home else c.ml_away
        bets.append(f"Moneyline: {e(c.ml_pick)}{' ' + american(q.price) if q else ''}")
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
  <table class="lines">
    <tr><th></th><th>Model</th><th>Market</th><th>Edge</th></tr>{"".join(rows)}
  </table>
  <p class="src">{_sources(c, book_names)}</p>
  <p class="meta">Prediction point: {e(POINT_LABELS.get(c.point, c.point))} {changed}</p>
  {f'<div class="why"><h4>Top factors (points for {e(c.home)})</h4><ul>{factors}</ul></div>'
   if factors else ''}
  {f'<div class="out"><h4>Likely out</h4><ul>{out_lines}</ul></div>' if out_lines else ''}
  {movement_html(c, movement)}
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


def records_html(rows: list[RecordRow]) -> str:
    if not rows:
        return "<p class='meta'>No graded picks yet.</p>"
    body = "".join(
        f"<tr><td>{escape(r.market)}</td><td>{r.wins}-{r.losses}-{r.pushes}</td>"
        f"<td>{'-' if r.roi is None else f'{100 * r.roi:+.1f}%'}</td>"
        f"<td>{'-' if r.avg_clv is None else f'{r.avg_clv:+.2f} {r.clv_unit}'}</td>"
        f"<td>{'-' if r.beat_close is None else f'{100 * r.beat_close:.0f}%'}</td>"
        f"<td class='ver'>{escape(r.model_version)}</td></tr>"
        for r in rows
    )  # fmt: skip
    return ("<table class='rec'><tr><th>Market</th><th>W-L-P</th><th>ROI</th><th>Avg CLV</th>"
            f"<th>Beat close</th><th>Model</th></tr>{body}</table>")  # fmt: skip


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


def sport_panel(sport: Sport, cards: list[Card], movement: dict, records: list[RecordRow],
                book_names: dict, info: RunInfo) -> str:  # fmt: skip
    games = "".join(card_html(c, book_names, movement.get(c.game_id, [])) for c in cards)
    if not games:
        games = "<p class='meta'>No games in the next 8 days.</p>"
    legend = "".join(f"<li>{escape(k)}: {escape(v)}</li>" for k, v in sport.thresholds().items())
    return f"""
<section class="games">{games}</section>
<section class="legend"><h2>Flags</h2><p>A pick is flagged when it clears the threshold chosen
  on validation seasons:</p><ul>{legend}</ul></section>
<section><h2>Season record</h2>{records_html(records)}</section>
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
    keys = status.get("missing_keys") or []
    keys_note = ("<p>Sources skipped because their API key is not set: "
                 + escape(", ".join(keys)) + ".</p>") if keys else ""  # fmt: skip
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
    panels = {}
    for sport in active:
        panels[sport.key] = sport_panel(sport, sport.cards(conn, now),
                                        movement_for(conn, sport, info), sport.records(conn),
                                        book_names, info)  # fmt: skip
    status = status_payload(info, now, missing_keys(settings))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render(entries, panels, status))
    (out_dir / "status.json").write_text(json.dumps(status, indent=1))
    (out_dir / ".nojekyll").write_text("")
    return status


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
.flags{{margin:6px 0}} .flag{{display:inline-block;background:var(--flag);color:var(--acc);
border-radius:6px;padding:2px 8px;margin:2px 4px 2px 0;font-size:.85rem;font-weight:700}}
table{{width:100%;border-collapse:collapse;font-size:.9rem}}
th,td{{text-align:left;padding:4px 4px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{color:var(--mut);font-weight:600}}
.src,.meta{{color:var(--mut);font-size:.8rem;margin:6px 0 0}}
ul{{margin:2px 0;padding-left:18px;font-size:.9rem}}
details{{margin-top:8px;font-size:.9rem}} summary{{cursor:pointer;color:var(--acc)}}
.rec .ver{{font-size:.7rem;color:var(--mut);word-break:break-all}}
.soon{{padding:24px 0;color:var(--mut)}}
footer{{color:var(--mut);font-size:.8rem;padding:24px 16px 32px}}
</style></head>
<body>
<header class="top"><h1>betdata · lines</h1>
<p class="updated">Last updated: <span id="updated">{updated}</span></p>
<div id="warn" hidden></div></header>
<main><nav role="tablist">{tabs}</nav>{panels}</main>
<footer><p><b>{footer}</b></p>{keys}
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
    if(s.rebuilt_at&&(Date.now()-new Date(s.rebuilt_at))<{rebuilt_days}*864e5)msg.push(
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
""".replace("{rebuilt_days}", str(REBUILT_NOTICE_DAYS))
