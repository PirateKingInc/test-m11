"""M17 Part G: the "Engine vs market" section of the NFL tab, from stored nfle17/nflpe17 rows
and the current quotes only (no model runs at page time).

Per game: the engine's spread and total with their 10th-90th range, the current market line,
the gap and P(cover) at that line (from the stored margin histogram), the three chain steps
that move the margin most, and a flag only when the gap clears the validated threshold.
Players with a stored prop line: the engine median, line, P(over) and edge; prop flags only for
markets whose thresholds were chosen on 2024 and earned on 2025.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape

import numpy as np
from sqlalchemy import Connection, and_, func, select

STEP_LABEL = {"pace": "pace", "pass_tendency": "pass rate", "sacks": "sacks/scrambles",
              "pass_offence": "passing offence", "pass_defence": "pass defence",
              "run_offence": "rushing offence", "run_defence": "run defence",
              "availability": "injuries", "home": "home field"}
MARKET = {"pass_yds": "pass yds", "rush_yds": "rush yds", "rec_yds": "rec yds",
          "receptions": "receptions", "anytime_td": "anytime TD"}


def _spread(home: str, away: str, home_margin: float) -> str:
    if abs(home_margin) < 0.25:
        return "pick'em"
    return f"{home if home_margin > 0 else away} −{abs(home_margin):.1f}"


def p_cover(hist: list[int], spread_home: float) -> float | None:
    h = np.asarray(hist, float)
    m = np.arange(len(h)) - 60
    v = m + spread_home
    win, lose = h[v > 0].sum(), h[v < 0].sum()
    return float(win / (win + lose)) if win + lose else None


def rows(conn: Connection, now: datetime, days: int = 8) -> list[dict]:
    from betdata.core.matching import sport_id
    from betdata.db.schema import games, model_predictions, teams
    from betdata.nfl.engine.live import MODEL
    from betdata.quotes import current_quotes

    mp = model_predictions
    h, a = teams.alias("h"), teams.alias("a")
    sid = sport_id(conn, "nfl")
    last = (select(mp.c.game_id, func.max(mp.c.predicted_at).label("at"))
            .join(games, games.c.game_id == mp.c.game_id)
            .where(mp.c.model == MODEL, mp.c.predicted_at <= now, games.c.sport_id == sid,
                   games.c.status == "scheduled", games.c.start_time_utc > now,
                   games.c.start_time_utc <= now + timedelta(days=days))
            .group_by(mp.c.game_id).subquery())  # fmt: skip
    q = (select(mp, games.c.start_time_utc, h.c.abbrev.label("home"), a.c.abbrev.label("away"))
         .join(last, and_(last.c.game_id == mp.c.game_id, last.c.at == mp.c.predicted_at))
         .join(games, games.c.game_id == mp.c.game_id)
         .join(h, h.c.team_id == games.c.home_team_id)
         .join(a, a.c.team_id == games.c.away_team_id)
         .where(mp.c.model == MODEL).order_by(games.c.start_time_utc, mp.c.game_id))  # fmt: skip
    out = [dict(r) for r in conn.execute(q).mappings()]
    quotes = current_quotes(conn, [r["game_id"] for r in out], now)
    for r in out:
        qq = quotes.get(r["game_id"], {})
        r["spread_now"] = qq.get("spread_home")
        r["total_now"] = qq.get("total")
    return out


def props(conn: Connection, now: datetime, days: int = 8) -> dict[int, list[dict]]:
    from betdata.db.schema import games, prop_predictions
    from betdata.nfl.engine.live import PROP_MODEL, roster_names

    pp = prop_predictions
    last = (select(pp.c.game_id, pp.c.player_id, pp.c.market,
                   func.max(pp.c.predicted_at).label("at"))
            .join(games, games.c.game_id == pp.c.game_id)
            .where(pp.c.model == PROP_MODEL, pp.c.predicted_at <= now,
                   games.c.status == "scheduled", games.c.start_time_utc > now,
                   games.c.start_time_utc <= now + timedelta(days=days))
            .group_by(pp.c.game_id, pp.c.player_id, pp.c.market).subquery())  # fmt: skip
    q = (select(pp).join(last, and_(last.c.game_id == pp.c.game_id,
                                    last.c.player_id == pp.c.player_id,
                                    last.c.market == pp.c.market,
                                    last.c.at == pp.c.predicted_at))
         .where(pp.c.model == PROP_MODEL).order_by(pp.c.player_name, pp.c.market))  # fmt: skip
    out: dict[int, list[dict]] = {}
    names = roster_names(conn, now.year - 1)
    for r in conn.execute(q).mappings():
        out.setdefault(r["game_id"], []).append(display_name(dict(r), names))
    return out


def display_name(row: dict, names: dict[str, str]) -> dict:
    """Rows stored before names were filled carry the ESPN id as the name; show the roster
    name of the row's gsis id instead (the stored row is not changed)."""
    if row["player_name"] in (None, row["player_id"]):
        gsis = (row.get("dist") or {}).get("gsis_id")
        row["player_name"] = names.get(gsis) or row["player_name"]
    return row


def section_html(game_rows: list[dict], prop_rows: dict[int, list[dict]], design,
                 et) -> str:  # fmt: skip
    """`design`: EngineDesign (thresholds); `et`: the page's Eastern-time formatter."""
    e = escape
    t = design.spread_threshold
    intro = ("<p class='meta'>nfle17 engine (market-free): plays, pass rate, dropbacks, targets, "
             "efficiency and drives simulated 10,000 times per game; no betting line is an input. "
             "Gap = engine margin vs the current line, in points toward the team named. "
             + (f"Flag: gap ≥ {t:g} pts (chosen on 2020-2022)." if t is not None
                and design.flag_sides else
                f"Info only, no side flags: the {t:g}-pt threshold chosen on 2020-2022 lost on "
                f"the test seasons (ROI {100 * design.side_test_roi:+.1f}%); its picks are still "
                "graded in the season record." if t is not None and design.side_test_roi
                is not None else "No side flags: no threshold earned one.") + "</p>")
    if not game_rows:
        return ("<section class='engine'><h2>Engine vs market</h2>" + intro
                + "<p class='meta'>No engine predictions for upcoming games yet.</p></section>")
    cards = []
    for r in game_rows:
        home, away = r["home"] or "?", r["away"] or "?"
        prm = r["params"] or {}
        m = r["pred_home_margin"]
        rng = (f"{_spread(home, away, prm.get('margin_p90', m))} … "
               f"{_spread(home, away, prm.get('margin_p10', m))}")
        tot = r["pred_total"]
        line = r["spread_now"]
        mkt, gap_txt, cover, flag = "no line", "", "", ""
        if line is not None and line.line is not None:
            mkt = _spread(home, away, -line.line)
            gap = m + line.line
            gap_txt = f" · gap {abs(gap):.1f} toward {e(home if gap > 0 else away)}"
            pc = p_cover(prm.get("margin_hist") or [], line.line)
            if pc is not None:
                side_p = pc if gap > 0 else 1 - pc
                cover = f" · P(cover {e(home if gap > 0 else away)}) {100 * side_p:.0f}%"
            if t is not None and design.flag_sides and abs(gap) >= t:
                flag = (f'<span class="flag">Engine: {e(home if gap > 0 else away)} '
                        f"{line.line if gap > 0 else -line.line:+g}</span>")
        steps = (r["factors"] or {}).get("steps") or {}
        top = sorted(steps.items(), key=lambda kv: -abs(kv[1].get("margin", 0)))[:3]
        top_txt = ", ".join(
            f"{STEP_LABEL.get(k, k)} {e(home if v['margin'] > 0 else away)} "
            f"+{abs(v['margin']):.1f}" for k, v in top if abs(v.get("margin", 0)) >= 0.1)
        tline = "" if r["total_now"] is None else f" · market {r['total_now'].line:g}"
        plist = []
        for p in prop_rows.get(r["game_id"], []):
            pfx = "–" if p["p_over"] is None else f"{100 * p['p_over']:.0f}%"
            edge = "" if p["edge"] is None else f" · edge {p['edge']:+.1f} pp"
            info = ("" if p["market"] in (design.prop_thresholds or {}) else
                    " <span class='research'>info only</span>")
            pflag = ""
            th = (design.prop_thresholds or {}).get(p["market"])
            if th is not None and p["edge"] is not None and abs(p["edge"]) >= 100 * th:
                pflag = (f' <span class="flag">{"Over" if p["edge"] > 0 else "Under"} '
                         f'{p["line"]:g}</span>')
            ln = "–" if p["line"] is None else f"{p['line']:g}"
            plist.append(f"<li>{e(p['player_name'])} · {MARKET.get(p['market'], p['market'])}: "
                         f"median <b>{p['mean']:.1f}</b> · line {ln} · P(over) {pfx}{edge}"
                         f"{pflag}{info}</li>")  # fmt: skip
        props_html = (f"<ul class='plist'>{''.join(plist)}</ul>" if plist else "")
        cards.append(
            f"<article class='game'><header><h3>{e(away)} @ {e(home)}</h3>"
            f"<time>{e(et(r['start_time_utc']))}</time></header>"
            f"<div>Engine <b>{_spread(home, away, m)}</b> (10–90%: {rng}) · market {mkt}"
            f"{gap_txt}{cover} {flag}</div>"
            f"<div>Total <b>{tot:.1f}</b> (10–90%: {prm.get('total_p10', tot):.0f}–"
            f"{prm.get('total_p90', tot):.0f}){tline}</div>"
            + (f"<div class='meta'>Biggest steps: {top_txt}</div>" if top_txt else "")
            + f"<div class='src'>{e(r['prediction_point'] or '')}, "
            f"{e(et(r['predicted_at'], '%a %-I:%M %p'))}</div>{props_html}</article>")  # fmt: skip
    return ("<section class='engine'><h2>Engine vs market</h2>" + intro + "".join(cards)
            + "</section>")


def engine_html(conn: Connection, now: datetime, et) -> str | None:
    from betdata.nfl.engine.live import DESIGN_PATH, EngineDesign

    if not DESIGN_PATH.exists():
        return None
    return section_html(rows(conn, now), props(conn, now), EngineDesign.load(), et)
