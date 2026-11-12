"""The Action card (owner decision 2026-11-12, MODEL.md "Action card"; config/cards.yaml
`action`): the best picks OG did NOT make official, every NBA card day from Nov 13, under their
own clearly unofficial label and record. No official card changes.

Chosen right after OG Ranking v2 and the Top 3 shadow, at the same moment (`alerts/card.py`
`run_shadow`): from every OG candidate (F / C-NBA picks for the day's games, created by then,
still Live, any market) not on the official OG card and whose player-game-market is not on it,
one pick per player-game-market, ranked by F's calibrated probability (ties: shorter price,
earlier created_at), top 3, 0.25u flat, no labels, each with every rule it failed (Rules v2,
then Ranking v2), the first shown as its reason tag.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

CARD = "action"
SIZE = 3
STAKE = 0.25
START = date(2026, 11, 13)
SOURCE = "action"
FIELDS = ("pick_id", "sport", "card_date", "action", "action_stake", "selected_at", "rank",
          "market", "player", "fair_prob", "fair_prob_cal", "price", "reason",
          "reasons")  # fmt: skip
# the reason tags, in the order shown (Rules v2, then Ranking v2)
REBOUNDS = "rebounds"
NON_ASSIST = "non-assist market"
UNDER_55 = "under 5.5+ assists"
TEAMMATES = "teammates out"
NO_PROB = "no model probability"
PRICE = "outside price range"
NO_MEDIAN = "no median"
MEDIAN = "median wrong side"
DUPLICATE = "duplicate player"
BELOW = "ranked below top 5"
ORDER = (REBOUNDS, NON_ASSIST, UNDER_55, TEAMMATES, NO_PROB, PRICE, NO_MEDIAN, MEDIAN, DUPLICATE,
         BELOW)  # fmt: skip
MARKET_SHORT = {"rebounds": "reb", "assists": "ast", "points": "pts", "threes": "3pm",
                "pra": "pra", "steals": "stl", "blocks": "blk"}


def key(day: date | str) -> str:
    return f"action:nba:{day if isinstance(day, str) else day.isoformat()}"


def active() -> bool:
    from betdata import cards as C

    c = C.load().get(CARD)
    return c is not None and c.active


def applies(day: date) -> bool:
    return day >= START and active()


def pgm(game_id, player, market) -> tuple:
    """Player-game-market: never twice across OG and Action, nor within Action."""
    return (int(game_id), (player or "").lower(), market)


def rules_v2_reasons(a, m=None) -> list[str]:
    """Every OG Rules v2 rule the pick fails (betdata/eligibility.py, applied rule by rule)."""
    from betdata import eligibility as EL

    out = []
    if a.market == "rebounds":
        out.append(REBOUNDS)
    elif a.market != "assists":
        out.append(NON_ASSIST)
    if a.market == "assists" and a.side == "under":
        if a.line is not None and a.line >= EL.UNDER_LINE - 1e-9:
            out.append(UNDER_55)
        tm = None
        try:
            from betdata.alerts.grade import utc

            tm = EL.teammates_out(m, a.game_id, (a.details or {}).get("team_id"),
                                  a.player_name, utc(a.created_at))  # fmt: skip
        except Exception:  # noqa: BLE001 - unknown: the rule cannot apply
            tm = None
        if tm is not None and tm >= EL.TEAMMATES_MAX:
            out.append(TEAMMATES)
    return out


def ranking_v2_reasons(a) -> list[str]:
    """Every Ranking v2 price / median rule the pick fails (betdata/og_ranking.py)."""
    from betdata import og_ranking as OR

    out = []
    if a.fair_prob is None:
        return [NO_PROB]
    if a.price is None or not OR.PRICE_MIN <= int(a.price) <= OR.PRICE_MAX:
        out.append(PRICE)
    med = OR.median_of(a)
    if a.side not in ("over", "under") or med is None or a.line is None:
        out.append(NO_MEDIAN)
    elif (a.side == "over" and not med > a.line) or (a.side == "under" and not med < a.line):
        out.append(MEDIAN)
    return out


def candidates(p, day: date, now: datetime, og: dict, m=None) -> tuple[list, int]:
    """(Action candidates with their reasons, OG candidates seen): every OG-signal pick for the
    day's games created by `now`, not started, Live at `now`, not on the OG card and not a
    player-game-market on it."""
    from betdata import og_ranking as OR
    from betdata.alerts.card import V1_SIGNALS, day_picks
    from betdata.alerts.grade import utc
    from betdata.alerts.labels import available

    on_og = {x["dedup_key"] for x in og.get("picks", [])}
    rejected = og.get("og_rejected") or {}
    pool = [a for a in day_picks(p, "nba", day, V1_SIGNALS)
            if utc(a.created_at) <= now and a.tip is not None and a.tip > now]  # fmt: skip
    og_pgm = {pgm(a.game_id, a.player_name, a.market) for a in pool if a.dedup_key in on_og}
    out, seen = [], 0
    for a in pool:
        if available(p, a, now) is not True:
            continue
        seen += 1
        if a.dedup_key in on_og or pgm(a.game_id, a.player_name, a.market) in og_pgm:
            continue
        why = rules_v2_reasons(a, m) + ranking_v2_reasons(a)
        if not why:  # eligible for OG, but not on its card
            why = [DUPLICATE if rejected.get(a.dedup_key) == OR.DUPLICATE else BELOW]
        a.reasons = [r for r in ORDER if r in why]
        out.append(a)
    return out, seen


def _key(a, fit):
    from betdata.alerts.grade import utc
    from betdata.alerts.rules import decimal
    from betdata.f_calibration import calibrate

    p = -1.0 if a.fair_prob is None else calibrate(a.fair_prob, fit)
    return (-p, decimal(int(a.price)) if a.price else 99.0, utc(a.created_at))


def choose(p, day: date, now: datetime, og: dict, m=None) -> dict:
    """The Action card at `now` (pure on the poller database; `og`: the OG card just chosen)."""
    from functools import partial

    from betdata import og_ranking as OR
    from betdata.f_calibration import calibrate

    fit = OR.f_fit(day)
    cands, seen = candidates(p, day, now, og, m)
    # owner 2026-11-12: rank by the raw model probability across markets (threes have no F
    # fit; for rebounds and assists the order is the same, the fit being increasing); each pick
    # still stores its calibrated value where one exists
    k = partial(_key, fit=None)
    best: dict = {}
    for a in sorted(cands, key=k):  # one per player-game-market: the best by the key
        best.setdefault(pgm(a.game_id, a.player_name, a.market), a)
    ranked = sorted(best.values(), key=k)
    picks = [{"dedup_key": a.dedup_key, "label": "", "stake": STAKE,
              "fair_prob": None if a.fair_prob is None else round(float(a.fair_prob), 4),
              "fair_prob_cal": None if a.fair_prob is None
              else round(calibrate(a.fair_prob, fit), 4),
              "price": int(a.price), "market": a.market, "player": a.player_name,
              "side": a.side, "line": a.line, "reasons": a.reasons, "reason": a.reasons[0]}
             for a in ranked[:SIZE]]  # fmt: skip
    counts: dict = {}
    for a in cands:
        counts[a.reasons[0]] = counts.get(a.reasons[0], 0) + 1
    out = {"sport": "nba", "day": day.isoformat(), "selected_at": now.isoformat(),
           "source": SOURCE, "picks": picks, "candidates": len(ranked), "seen": seen,
           "reasons": counts}
    if og.get("no_lines"):
        out["no_lines"] = True
    if fit is not None:
        out["f_fit"] = fit.fit_date
    return out


# --- the ledger ------------------------------------------------------------------------------


def ledger_rows(p, out: Path, now: datetime) -> int:
    """Write-once rows of ledger/action_card.csv (one per Action pick, referencing the existing
    pick row) and a card-day row (kind `action`, also when empty)."""
    from sqlalchemy import select

    from betdata.alerts.db import state
    from betdata.ledger import CARD_DAY_FIELDS, _num, append_rows, pick_id

    rows, days = [], []
    for st in p.execute(select(state).where(state.c.key.like("action:%"))):
        c = st.value or {}
        if not c.get("day"):
            continue
        for i, x in enumerate(c.get("picks", []), 1):
            rows.append({"pick_id": pick_id(x["dedup_key"]), "sport": "nba",
                         "card_date": c["day"], "action": "yes",
                         "action_stake": _num(x.get("stake", STAKE)),
                         "selected_at": c.get("selected_at", ""), "rank": str(i),
                         "market": x.get("market", ""), "player": x.get("player") or "",
                         "fair_prob": _num(x.get("fair_prob")),
                         "fair_prob_cal": _num(x.get("fair_prob_cal")),
                         "price": x.get("price", ""), "reason": x.get("reason", ""),
                         "reasons": "; ".join(x.get("reasons", []))})  # fmt: skip
        days.append({"key": f"nba:{c['day']}:{CARD}", "sport": "nba", "card_date": c["day"],
                     "kind": CARD, "selected_at": c.get("selected_at", ""),
                     "picks": str(len(c.get("picks", [])))})  # fmt: skip
    n = append_rows(out / "action_card.csv", FIELDS, rows)
    append_rows(out / "card_days.csv", CARD_DAY_FIELDS, days, key="key")
    return n


def rows_of(out: Path) -> dict[str, dict]:
    from betdata.ledger import read_rows

    return {r["pick_id"]: r for r in read_rows(out / "action_card.csv")}


# --- display -------------------------------------------------------------------------------


def pick_line(x: dict) -> str:
    """'Ivica Zubac u9.5 reb +116 · 0.25u · missed: rebounds'."""
    from betdata.alerts.notify import _fmt_price

    side = {"over": "o", "under": "u"}.get(x.get("side") or "", (x.get("side") or "") + " ")
    line = "" if x.get("line") is None else f"{float(x['line']):g}"
    mk = MARKET_SHORT.get(x.get("market") or "", x.get("market") or "")
    return (f"{x.get('player') or '?'} {side}{line} {mk} {_fmt_price(int(x['price']))} · "
            f"{float(x.get('stake', STAKE)):g}u · missed: {x.get('reason', '?')}")


def push_lines(card: dict | None) -> list[str]:
    """The push section after Spread picks (owner 2026-11-12)."""
    if not active():
        return []
    if card is None:
        return ["Action picks: chosen with the OG card"]
    if not card.get("picks"):
        return ["Action picks: none today"]
    out = ["Action picks (not official):", *[f"  {pick_line(x)}" for x in card["picks"]]]
    why = note(card)  # a short card says why (owner 2026-11-12)
    return out + ([f"  {why.split(': ', 1)[1]}"] if why else [])


def note(card: dict | None) -> str:
    """Tonight's card line of a short or empty Action card ('' when full)."""
    if card is None:
        return ""
    n = len(card.get("picks", []))
    if n >= SIZE:
        return ""
    if card.get("no_lines"):
        return "Action: no picks — no lines posted in time"
    c = card.get("candidates", 0)
    if n == 0:
        return "Action: no picks — every OG candidate is on the OG card" if card.get("seen") \
            else "Action: no picks — no OG candidates today"
    return f"Action: {n} pick{'s' if n != 1 else ''} — only {c} candidate{'s' if c != 1 else ''}" \
        " off the OG card"


# --- the record (graded only, 0.25u) --------------------------------------------------------


def breakdown(rows: list[dict], by: str) -> dict:
    """{tag: Rec} of the Action card's graded picks by reason tag (`reason`) or `market`; the
    parts sum to the whole record."""
    from betdata import official as O

    groups: dict = {}
    for r in O.graded(rows):
        k = r.get("action_reason") if by == "reason" else r.get("market")
        groups.setdefault(k or "?", []).append(r)
    return {k: O.record(v, O.official_stake)
            for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}  # fmt: skip


# --- live adds (owner 2026-11-12): picks posted after the morning card ------------------------

ADDS = 3  # live adds a day, at most
ADD_MIN_LEAD_MIN = 30  # the game must tip more than 30 minutes after the sweep
ADD_HOURS = (6, 23)  # Eastern: no live adds from 11 PM to 6 AM
OG_NOTE = "Would have made OG (posted after the morning card)"
POSTED_AFTER = "posted after the morning card"
ADDS_FIELDS = ("pick_id", "sport", "card_date", "add", "stake", "posted_at", "posted",
               "og_quality", "note", "market", "player", "side", "line", "price", "book",
               "fair_prob", "fair_prob_cal", "reason", "reasons", "push")  # fmt: skip


def adds_key(day) -> str:
    return f"actionadds:nba:{day if isinstance(day, str) else day.isoformat()}"


def _et(t: datetime) -> str:
    from betdata.alerts.rules import EASTERN

    return t.astimezone(EASTERN).strftime("%-I:%M %p ET")


def evaluate_adds(p, day, now: datetime, og: dict, ac: dict, st: dict,
                  m=None) -> tuple[list, dict]:  # fmt: skip
    """(new adds, log entry): every OG-signal pick created since the last evaluation (and after
    the morning Action card) for the day's games, judged once (module docstring, MODEL.md)."""
    from betdata import eligibility as EL
    from betdata import og_ranking as OR
    from betdata.alerts.card import V1_SIGNALS, day_picks
    from betdata.alerts.grade import utc
    from betdata.alerts.labels import available
    from betdata.f_calibration import calibrate

    since = datetime.fromisoformat(st.get("evaluated_through") or ac["selected_at"])
    new = [a for a in day_picks(p, "nba", day, V1_SIGNALS)
           if since < utc(a.created_at) <= now]  # fmt: skip
    adds = st.get("adds", [])
    taken = {pgm(x["game_id"], x["player"], x["market"]) for x in adds}
    by_key = {a.dedup_key: a for a in day_picks(p, "nba", day, V1_SIGNALS)}
    for x in [*og.get("picks", []), *ac.get("picks", [])]:
        a = by_key.get(x["dedup_key"])
        if a is not None:
            taken.add(pgm(a.game_id, a.player_name, a.market))
    og_probs = sorted((x.get("fair_prob") or 0 for x in og.get("picks", [])), reverse=True)
    og_bar = og_probs[4] if len(og_probs) >= 5 else None
    ac_probs = sorted((x.get("fair_prob") or 0 for x in ac.get("picks", [])), reverse=True)
    ac_bar = ac_probs[2] if len(ac_probs) >= 3 else None
    why_not: dict = {}
    ok = []
    for a in new:
        if a.tip is None or a.tip <= now + timedelta(minutes=ADD_MIN_LEAD_MIN):
            why_not["tips within 30 minutes"] = why_not.get("tips within 30 minutes", 0) + 1
            continue
        if pgm(a.game_id, a.player_name, a.market) in taken:
            why_not["already on OG / Action"] = why_not.get("already on OG / Action", 0) + 1
            continue
        if a.fair_prob is None:
            why_not["no model probability"] = why_not.get("no model probability", 0) + 1
            continue
        if available(p, a, now) is not True:
            why_not["price no longer Live"] = why_not.get("price no longer Live", 0) + 1
            continue
        p_ = float(a.fair_prob)
        rules_ok = EL.alert_reason(m, a) is None and OR.reason(a) is None
        og_quality = rules_ok and (og_bar is None or p_ >= og_bar)
        beats = ac_bar is None or p_ >= ac_bar
        if not (og_quality or beats):
            why_not["below the bar"] = why_not.get("below the bar", 0) + 1
            continue
        ok.append((og_quality, p_, a))
    ok.sort(key=lambda t: (not t[0], -t[1]))
    out = []
    fit = OR.f_fit(day)
    for og_q, p_, a in ok:
        if len(adds) + len(out) >= ADDS:
            why_not["daily cap reached"] = why_not.get("daily cap reached", 0) + 1
            continue
        k = pgm(a.game_id, a.player_name, a.market)
        if k in taken:
            continue
        taken.add(k)
        why = rules_v2_reasons(a, m) + ranking_v2_reasons(a)
        reasons = [r for r in ORDER if r in why] or [POSTED_AFTER if og_q else BELOW]
        cal = calibrate(p_, fit) if a.market in ("rebounds", "assists") else None
        out.append({"dedup_key": a.dedup_key, "game_id": int(a.game_id), "label": "",
                    "stake": STAKE, "fair_prob": round(p_, 4),
                    "fair_prob_cal": None if cal is None else round(cal, 4),
                    "price": int(a.price), "market": a.market, "player": a.player_name,
                    "side": a.side, "line": a.line, "book": a.book,
                    "reasons": reasons, "reason": reasons[0], "og_quality": og_q,
                    "note": OG_NOTE if og_q else "", "posted_at": now.isoformat(),
                    "posted": f"live {_et(now)}"})  # fmt: skip
    log = {"at": now.isoformat(), "new": len(new), "added": [x["dedup_key"] for x in out],
           "why_not": why_not}
    return out, log


def add_line(x: dict) -> str:
    """The push / page line of a live add."""
    return pick_line(x) + (f" · {OG_NOTE}" if x.get("og_quality") else "")


def live_adds(pdb, main, now: datetime, topic: str | None, client=None) -> list[dict]:
    """One poller cycle's live adds (owner 2026-11-12): after the morning Action card, 6 AM -
    11 PM ET; each new add pushed once ("Action add (not official)"). Returns the new adds."""
    import httpx

    from betdata.alerts.card import shadow_key
    from betdata.alerts.db import get_state, set_state
    from betdata.alerts.notify import NTFY, PAGE
    from betdata.alerts.rules import EASTERN

    t = now.astimezone(EASTERN)
    day = t.date()
    if not applies(day) or not ADD_HOURS[0] <= t.hour < ADD_HOURS[1]:
        return []  # quiet hours: nothing is evaluated, nothing pushed
    with pdb.connect() as p:
        ac = get_state(p, key(day))
        og = get_state(p, shadow_key("nba", day)) or {}
        st = get_state(p, adds_key(day)) or {}
    if ac is None or not ac.get("selected_at"):
        return []  # the morning Action card first
    with pdb.connect() as p, main.connect() as m:
        new, log = evaluate_adds(p, day, now, og, ac, st, m)
    hist = st.get("log", [])
    last = datetime.fromisoformat(hist[-1]["at"]) if hist else None
    if log["new"] or new or last is None or now - last >= timedelta(minutes=55):
        hist = [*hist[-99:], log]  # every evaluation with new picks, and an hourly heartbeat
    for x in new:
        if not topic:
            x["push"] = "no NTFY_TOPIC"
            continue
        try:
            r = (client or httpx.Client(timeout=15)).post(
                NTFY + topic, content=add_line(x).encode(),
                headers={"Title": "Action add (not official)", "Click": PAGE,
                         "Tags": "heavy_plus_sign"})
            x["push"] = f"http {r.status_code}"
        except Exception as e:  # noqa: BLE001 - a failed push never loses the add
            x["push"] = f"error {type(e).__name__}"
    with pdb.begin() as p:
        set_state(p, adds_key(day), {"day": day.isoformat(), "adds": [*st.get("adds", []), *new],
                                     "evaluated_through": now.isoformat(), "log": hist}, now)
    return new


def adds_ledger_rows(p, out: Path, now: datetime) -> int:
    """Write-once rows of ledger/action_adds.csv (one per live add, referencing its pick row)."""
    from sqlalchemy import select

    from betdata.alerts.db import state
    from betdata.ledger import _num, append_rows, pick_id

    rows = []
    for st in p.execute(select(state).where(state.c.key.like("actionadds:%"))):
        c = st.value or {}
        for x in c.get("adds", []):
            rows.append({"pick_id": pick_id(x["dedup_key"]), "sport": "nba",
                         "card_date": c.get("day", ""), "add": "yes",
                         "stake": _num(x.get("stake", STAKE)), "posted_at": x.get("posted_at", ""),
                         "posted": x.get("posted", ""),
                         "og_quality": "true" if x.get("og_quality") else "false",
                         "note": x.get("note", ""), "market": x.get("market", ""),
                         "player": x.get("player") or "", "side": x.get("side", ""),
                         "line": _num(x.get("line"), 2), "price": x.get("price", ""),
                         "book": x.get("book", ""), "fair_prob": _num(x.get("fair_prob")),
                         "fair_prob_cal": _num(x.get("fair_prob_cal")),
                         "reason": x.get("reason", ""), "reasons": "; ".join(x.get("reasons", [])),
                         "push": x.get("push", "")})  # fmt: skip
    return append_rows(out / "action_adds.csv", ADDS_FIELDS, rows)



def sub_records(rows: list[dict]) -> dict:
    """The Action record's parts (owner 2026-11-12): Morning, Live adds and, of the live adds,
    Would have made OG; graded picks only (Morning + Live adds = the whole)."""
    from betdata import official as O

    live = [r for r in rows if str(r.get("action_posted", "")).startswith("live")]
    morning = [r for r in rows if not str(r.get("action_posted", "")).startswith("live")]
    return {"Morning": O.record(morning, O.official_stake),
            "Live adds": O.record(live, O.official_stake),
            "Would have made OG": O.record([r for r in live if r.get("action_og")],
                                           O.official_stake)}  # fmt: skip
