"""Today's tracker (owner request 2026-10-31, MODEL.md "Today's tracker"): display only, no
change to picks, labels, rules or grading.

`build` turns today's picks (US Eastern day) into a small JSON document, grouped by game:
- each game: label, tip, status (Scheduled / In progress / Final) and, where known, the score;
- each pick: card or not, label and stake (the card's at selection, else the creation label),
  pick, odds, book; before the game its price status against the latest poll (Live / Moved /
  Gone, alerts/labels.py); after it the result in plain words with the actual stat for props,
  units at the pick's stake, and CLV once graded.

The poller rebuilds it every 5 minutes during game windows and pushes it to the `tracker` branch
(`publish`); the page renders it in the browser and re-reads it every 2 minutes. The hourly page
build embeds the same document, so the section works without the branch.

Live scores (PROJECT.md "Tracker sources"): balldontlie's free games endpoint, one request per
sport per refresh, only when the `BALLDONTLIE_API_KEY` secret is set. Without it a started game
shows "In progress" without a score. No free, terms-compliant live player stats exist, so player
results appear at the final, from the box score the existing grading uses.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from betdata.alerts.db import alert_grades, alerts, consensus_grades, get_state
from betdata.alerts.rules import EASTERN

log = logging.getLogger(__name__)
SPORTS = ("nba", "nfl")
EVERY = timedelta(minutes=5)
WINDOW_BEFORE = timedelta(minutes=30)
WINDOW_AFTER = {"nba": timedelta(hours=4), "nfl": timedelta(hours=5)}
BRANCH = "tracker"
FILE = "tracker.json"
# When results post (owner request 2026-11-04): a game's expected end is its tip plus a typical
# length; its result posts on the next hourly run at or after that (runs on the hour, 7am-10pm
# ET, the page out ~15 minutes later); after the last run of the day, the next morning.
GAME_LENGTH = {"nba": timedelta(hours=2, minutes=30), "nfl": timedelta(hours=3, minutes=15)}
POST_DELAY = timedelta(minutes=15)
OG_SWITCH_OVER = "No card today (switch-over)"  # OG on its excluded day (config/cards.yaml)
MORNING_ETA = (6, 15)  # "grading ~6:15 AM ET tomorrow" (the morning update, 2026-11-05)
BDL = {"nba": "https://api.balldontlie.io/v1/games",
       "nfl": "https://api.balldontlie.io/nfl/v1/games"}  # fmt: skip
# balldontlie abbreviations that differ from ours
ALIAS = {"WSH": "WAS", "BRK": "BKN", "NOH": "NOP", "NO": "NOP", "SA": "SAS", "GS": "GSW",
         "NY": "NYK", "UTAH": "UTA", "PHO": "PHX", "JAC": "JAX", "LA": "LAR"}
MARKET_WORD = {"points": "points", "rebounds": "rebounds", "assists": "assists",
               "threes": "threes", "rec_yds": "receiving yards", "rush_yds": "rushing yards",
               "receptions": "receptions", "pass_yds": "passing yards"}  # fmt: skip


def _utc(t):
    from betdata.alerts.grade import utc

    return None if t is None else utc(t)


def _abbr(x: str | None) -> str:
    x = (x or "").strip().upper()
    return ALIAS.get(x, x)


# --- live scores (balldontlie, free tier with a key) -----------------------------------------


def bdl_scores(sport: str, day: date, key: str | None, client=None) -> dict[tuple, dict]:
    """{(away, home): {"status", "home", "away", "period"}} from balldontlie; {} without a key
    or on any failure (the tracker then shows no score)."""
    if not key:
        return {}
    import httpx

    try:
        r = (client or httpx.Client(timeout=15)).get(
            BDL[sport], params={"dates[]": day.isoformat(), "per_page": 100},
            headers={"Authorization": key})
        r.raise_for_status()
        out = {}
        for g in r.json().get("data", []):
            st = str(g.get("status") or "")
            final = st.lower().startswith("final")
            started = final or bool(g.get("period")) or any(
                k in st.lower() for k in ("qtr", "half", "ot", "quarter"))
            out[(_abbr((g.get("visitor_team") or {}).get("abbreviation")),
                 _abbr((g.get("home_team") or {}).get("abbreviation")))] = {
                "state": "final" if final else "live" if started else "pre",
                "home": g.get("home_team_score"), "away": g.get("visitor_team_score"),
                "period": st if started and not final else ("Final" if final else ""),
                "clock": (g.get("time") or "").strip()}  # fmt: skip
        return out
    except Exception as e:  # noqa: BLE001 - scores are optional
        log.warning("balldontlie %s scores failed: %s", sport, e)
        return {}


# --- the document ----------------------------------------------------------------------------


def day_bounds(day: date) -> tuple[datetime, datetime]:
    from betdata.alerts.card import day_bounds as b

    return b(day)


def _final_score(m, game_id: int):
    if m is None:
        return None
    from betdata.db.schema import results

    r = m.execute(select(results.c.home_score, results.c.away_score)
                  .where(results.c.game_id == game_id, results.c.final.is_(True))
                  .order_by(results.c.source)).first()  # fmt: skip
    return None if r is None or r.home_score is None else (r.home_score, r.away_score)


def _actual(m, a) -> float | str | None:
    """The player's stat for an NBA prop (the box score the grading uses); None otherwise."""
    if m is None or not a.player_name or a.sport != "nba":
        return None
    from betdata.alerts.grade import prop_actual

    try:
        return prop_actual(m, a, None)
    except Exception:  # noqa: BLE001
        return None


def result_text(a, res: str, units: float, actual, score) -> str:
    """The final in plain words, e.g. "Anthony Black: 2 assists — under 3.5 WIN, +0.58u"."""
    from betdata.alerts.notify import selection

    tail = f"{res.upper()}, {units:+.2f}u"
    if res == "void":
        return "VOID (did not play)" if a.player_name else "VOID"
    if a.player_name:
        if isinstance(actual, float):
            n = int(actual) if actual == int(actual) else actual
            word = MARKET_WORD.get(a.market, a.market)
            word = word[:-1] if n == 1 and word.endswith("s") else word
            return f"{a.player_name}: {n} {word} — {a.side} {a.line:g} {tail}"
        return f"{selection(a)} — {tail}"
    if score is not None:
        home, away = score
        aw, hm = a.game_label.split(" @ ")
        if a.market == "totals":
            return f"Total {home + away} — {a.side} {a.line:g} {tail}"
        return f"{selection(a)}: final {aw} {away}-{home} {hm} — {tail}"
    return f"{selection(a)} — {tail}"


def et_time(t: datetime) -> str:
    """'7:00 PM ET'."""
    return t.astimezone(EASTERN).strftime("%-I:%M %p ET")


def grading_eta(tip: datetime, sport: str) -> dict:
    """Expected end, when the result should post, and the plain words for it:
    {"end", "grade_at" (None: tomorrow morning), "grade_text"}."""
    from betdata.runner.window import FIRST_HOUR, LAST_HOUR

    end = tip + GAME_LENGTH.get(sport, GAME_LENGTH["nba"])
    local = end.astimezone(EASTERN)
    run = local.replace(minute=0, second=0, microsecond=0)
    if run < local:
        run += timedelta(hours=1)
    same_day = run.date() == tip.astimezone(EASTERN).date()
    if same_day and FIRST_HOUR <= run.hour <= LAST_HOUR:
        at = run + POST_DELAY
        return {"end": end.isoformat(), "grade_at": at.astimezone(tip.tzinfo).isoformat(),
                "grade_text": f"grading by ~{et_time(at)}"}
    h, m = MORNING_ETA
    return {"end": end.isoformat(), "grade_at": None,
            "grade_text": f"grading ~{h}:{m:02d} AM ET tomorrow"}


def plain_status(tip: datetime | None, sport: str, now: datetime, final: bool = False,
                 score: str = "") -> str:
    """The status of a not-yet-graded pick, in plain words (never "pending"): "Tips 7:00 PM
    ET", "In progress" (with the score when known), "Final — grading by ~9:15 PM ET"."""
    if tip is None:
        return "Not graded yet"
    eta = grading_eta(tip, sport)
    if final or now >= datetime.fromisoformat(eta["end"]):
        return f"Final — {eta['grade_text']}"
    if now >= tip:
        return "In progress" + (f" {score}" if score else "")
    return f"Tips {et_time(tip)}"


def _teams(m, a, details: dict) -> tuple[str, str]:
    """(team, opponent) of a pick (betdata/review.py); blanks when unknown."""
    from betdata.review import teams_for

    try:
        return teams_for(m, {"game": a.game_label, "player": a.player_name or "",
                             "sport": a.sport, "game_id": a.game_id,
                             "created_at": _utc(a.created_at).isoformat(), "market": a.market,
                             "side": a.side}, details or {})  # fmt: skip
    except Exception:  # noqa: BLE001 - a missing team never drops the pick
        return "", ""


def build(p, m, sport: str, day: date, now: datetime, scores: dict | None = None) -> dict:
    """Today's tracker for one sport (module docstring). `p`: the poller database; `m`: the main
    database (final scores, box scores), or None."""
    from types import SimpleNamespace

    from betdata import cards as C
    from betdata.alerts.card import card_note, shadow_key, today_signals
    from betdata.alerts.card import key as card_key
    from betdata.alerts.gates import signal_name
    from betdata.alerts.labels import status
    from betdata.alerts.notify import BOOKS, MARKETS, _fmt_price, selection
    from betdata.ledger import pick_id

    lo, hi = day_bounds(day)
    card = get_state(p, card_key(sport, day)) or {}
    on_card = {x["dedup_key"]: x for x in card.get("picks", [])}
    og = get_state(p, shadow_key(sport, day)) or {}  # OG (owner 2026-11-05), NBA only
    switch_over = sport == "nba" and day.isoformat() in C.load()[C.OG].exclude_days
    if switch_over:  # Nov 5: no OG card (owner 2026-11-05); its picks stay research
        og = {}
    on_og = {x["dedup_key"]: x for x in og.get("picks", [])}
    signals = today_signals(sport, day)
    sp_card = None
    if sport == "nba":  # Spread Picks (owner 2026-11-10): shown only in the Spread view
        from betdata import spread as SP

        if SP.active():
            sp_card = get_state(p, SP.key(day))
            signals = (*signals, SP.SIGNAL)
    on_sp = {x["dedup_key"]: x for x in (sp_card or {}).get("picks", [])}
    trig_price = {}
    rows = p.execute(
        select(alerts, alert_grades.c.result, alert_grades.c.profit, consensus_grades.c.clv)
        .outerjoin(alert_grades, alert_grades.c.alert_id == alerts.c.alert_id)
        .outerjoin(consensus_grades, consensus_grades.c.alert_id == alerts.c.alert_id)
        .where(alerts.c.sport == sport, alerts.c.signal.in_(signals),
               alerts.c.tip >= lo, alerts.c.tip < hi)
        .order_by(alerts.c.tip, alerts.c.game_label, alerts.c.created_at)).mappings().all()
    games: dict[int, dict] = {}
    scores = scores or {}
    for r in rows:
        if r["signal"] == "engine":
            trig_price[r["dedup_key"]] = r["price"]
    for r in rows:
        a = SimpleNamespace(**{k: r[k] for k in r.keys()})
        a.tip = _utc(a.tip)
        if a.signal == "dogsprd" and a.dedup_key not in on_sp:
            continue  # only the stored Spread card's picks
        g = games.get(a.game_id)
        if g is None:
            aw, hm = (a.game_label or "? @ ?").split(" @ ")
            live = scores.get((_abbr(aw), _abbr(hm)))
            final = _final_score(m, a.game_id)
            if final is not None or (live and live["state"] == "final"):
                state = "final"
            elif live and live["state"] == "live":
                state = "live"
            elif live and live["state"] == "pre":
                state = "pre"
            else:
                state = "pre" if a.tip is None or now < a.tip else "live"
            sc = (final if final is not None else
                  (live["home"], live["away"]) if live and live.get("home") is not None
                  else None)  # fmt: skip
            eta = grading_eta(a.tip, sport) if a.tip else {}
            g = games[a.game_id] = {
                "game_id": a.game_id, "label": a.game_label, "tip": a.tip.isoformat()
                if a.tip else None, "state": state, "end": eta.get("end"),
                "grade_at": eta.get("grade_at"), "grade_text": eta.get("grade_text"),
                "score": None if sc is None else {"home": sc[0], "away": sc[1]},
                "period": (live or {}).get("period", ""), "clock": (live or {}).get("clock", ""),
                "picks": []}  # fmt: skip
        d = a.details or {}
        c = on_card.get(a.dedup_key)
        o = on_og.get(a.dedup_key)
        lab = c or o or d.get("label") or {}
        stake = float(lab.get("stake", 1.0)) if lab else 1.0
        res = a.result
        units = None if res is None or a.profit is None else round(stake * a.profit, 2)
        pick = {"id": pick_id(a.dedup_key), "card": c is not None, "og": o is not None,
                "og_label": (o or {}).get("label", ""), "og_stake": (o or {}).get("stake"),
                "bet": C.bet_key(a.game_id, a.market, a.player_name, a.side),
                "label": lab.get("label") or "", "stake": stake,
                "signal": signal_name(a.signal, a.sport),
                "pick": selection(a), "odds": _fmt_price(a.price),
                "book": BOOKS.get(a.book, a.book), "result": res, "units": units,
                "clv": None if a.clv is None else round(100 * a.clv, 1)}  # fmt: skip
        team, opp = _teams(m, a, d)
        pick.update(team=team, opp=opp, player=a.player_name or "",
                    market=MARKETS.get(a.market, a.market))  # M19: games and teams first
        if res is not None:
            sc = g["score"]
            pick["text"] = result_text(a, res, units or 0.0, _actual(m, a),
                                       None if sc is None else (sc["home"], sc["away"]))
            g["state"] = "final"  # a graded pick means the game is final
        elif g["state"] == "pre":
            try:
                pick["price"] = status(p, a, now)
            except Exception:  # noqa: BLE001 - a status never drops the pick
                pick["price"] = None
        s = on_sp.get(a.dedup_key)
        if a.signal == "dogsprd":  # Spread Picks: 1 unit flat, naming the ML pick it came from
            team = a.game_label.split(" @ ")[1 if a.side == "home" else 0]
            ml = trig_price.get((s or {}).get("trigger_key", ""))
            pick.update(spread=s is not None, label=(s or {}).get("label", "SPREAD"),
                        stake=float((s or {}).get("stake", 1.0)), card=False, og=False,
                        trigger="" if ml is None else
                        f"from Next Gen ML {team} {_fmt_price(int(ml))}")  # fmt: skip
            if res is not None and a.profit is not None:
                pick["units"] = round(pick["stake"] * a.profit, 2)
        g["picks"].append(pick)
    # the same bet on both NBA cards (found by each card's own signal): marked on both
    bets_ng = {x["bet"] for g in games.values() for x in g["picks"] if x["card"]}
    bets_og = {x["bet"] for g in games.values() for x in g["picks"] if x["og"]}
    for g in games.values():
        for x in g["picks"]:
            x["both"] = x["bet"] in bets_ng and x["bet"] in bets_og and (x["card"] or x["og"])
    out = {"sport": sport, "day": day.isoformat(), "generated_at": now.isoformat(),
           "scores": "balldontlie" if scores else None,
           "games": list(games.values())}  # fmt: skip
    if card.get("source") == "engine":  # the engine card (owner override 2026-11-04)
        out["card_source"] = "engine"
        out["card_note"] = card_note(card)
    if og:  # OG, the second official NBA card (owner 2026-11-05)
        out["og_chosen"] = True
        out["og_note"] = card_note(og)
    elif switch_over:
        out["og_note"] = OG_SWITCH_OVER
    if sport == "nba" and C.load():
        out["cards"] = {k: C.name(k) for k in (C.NEXTGEN, C.OG)}
    if sport == "nba" and sp_card is not None:  # Spread Picks (owner 2026-11-10)
        out["spread_chosen"] = True
        if not sp_card.get("picks"):
            sk = sp_card.get("skipped") or []
            out["spread_note"] = ("No spread picks today — no Next Gen ML underdog on the card"
                                  if not sk else "No spread picks today — "
                                  + "; ".join(x["reason"] for x in sk))  # fmt: skip
    if sport == "nba":  # the Next Gen parlay (owner 2026-11-06); never breaks the tracker
        try:
            from betdata.ledger import ledger_dir
            from betdata.parlay import page_doc

            pdoc = page_doc(p, m, day, ledger_dir())
            if pdoc is not None:
                out["parlay"] = pdoc
        except Exception as e:  # noqa: BLE001
            log.warning("parlay section failed: %s", e)
    return out


def document(p, m, now: datetime, scores_key: str | None = None, client=None) -> dict:
    day = now.astimezone(EASTERN).date()
    return {"generated_at": now.isoformat(), "day": day.isoformat(),
            "sports": {s: build(p, m, s, day, now, bdl_scores(s, day, scores_key, client))
                       for s in SPORTS}}  # fmt: skip


# --- when, and publishing ----------------------------------------------------------------------


def in_window(main, now: datetime, tip_fn=None) -> bool:
    """From 30 minutes before a sport's first game of the day to 4 (NFL 5) hours after its last."""
    from betdata.db.schema import games, sports

    day = now.astimezone(EASTERN).date()
    lo, hi = day_bounds(day)
    for sport in SPORTS:
        with main.connect() as m:
            tips = [_utc(t) for (t,) in m.execute(
                select(games.c.start_time_utc).join(sports, sports.c.sport_id == games.c.sport_id)
                .where(sports.c.key == sport, games.c.start_time_utc >= lo - WINDOW_AFTER[sport],
                       games.c.start_time_utc < hi))]  # fmt: skip
        if any(t - WINDOW_BEFORE <= now <= t + WINDOW_AFTER[sport] for t in tips):
            return True
    return False


def publish(doc: dict, repo: str | None = None, token: str | None = None,
            remote: str | None = None) -> str:  # fmt: skip
    """Force-push the document as the single commit of the `tracker` branch (never main).
    `remote`: another push URL (tests)."""
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    token = token or os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        return "no GITHUB_TOKEN"
    import base64

    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    with tempfile.TemporaryDirectory() as d:
        Path(d, FILE).write_text(json.dumps(doc, separators=(",", ":")))
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        bot = ["-c", "user.name=github-actions[bot]",
               "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com"]
        steps = [("init", ["git", "init", "-q", "-b", BRANCH]),
                 ("add", ["git", "add", FILE]),
                 ("commit", ["git", *bot, "commit", "-q", "-m",
                             f"tracker {doc['generated_at']}"]),
                 ("push", ["git", "-c", f"http.extraheader=AUTHORIZATION: basic {auth}",
                           "push", "-q", "-f", remote or f"https://github.com/{repo}.git",
                           f"{BRANCH}:{BRANCH}"])]  # fmt: skip
        for name, cmd in steps:
            r = subprocess.run(cmd, cwd=d, capture_output=True, text=True, env=env)
            if r.returncode:
                return f"git {name} failed"  # never the token or its output
    return "pushed"
