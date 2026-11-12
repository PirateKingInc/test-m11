"""Tonight's card (owner request 2026-10-30, MODEL.md "Tonight's card"): an in-house
simulation of a pick service. Display only: no signal, threshold, model or grade changes.

Once a day per sport, at 6:15 AM US Eastern on days that sport has games, the poller selects
the card: the top 5 picks by expected value among that day's M18 picks (C, F, G2, G1) still
"Live" (the alerted price, or better, is the book's latest stored quote) for games not yet
started. If none is Live at 6:15, the card is selected at the first poll that day with any Live
pick. Picks created after selection stay off the card; fewer than 5 qualifying means a shorter
card. Each card pick gets a label and stake at selection:
- **TAKE** (1 unit): edge >= 1.5x its signal's threshold AND (edge source "news" OR a fresh
  Pinnacle price sides with the pick);
- **LEAN** (0.5 unit): every other card pick; book disagreement against a two-book consensus is
  LEAN at most.
(The same tests as alerts/labels.py, at selection time; the price is Live by construction.)

Expected value per unit staked = fair probability x decimal odds - 1. A pick with no fair
probability (G1: its rule is a points drop, not a priced edge) has no expected value and is never
on the card.

Right after selection the card is pushed to ntfy, one message per sport. The selection is stored
once in the poller state (`card:<sport>:<day>`), saved with the poller database at :56, and
written once to the ledger (`ledger/card.csv`, betdata/ledger.py) by the next hourly run, which
publishes it. Since 2026-11-05 the morning update (betdata/morning.py) selects at 6:15 AM ET
(it replaced the 8:50 AM selection) and publishes at once. A card prop whose player does not
play (ruled
out) is graded void, as books do (alerts/grade.py `prop_actual`); a void is kept but stays out of
W-L-P, units and ROI. Cards start on 2026-10-31 (no backfill).

Engine card (owner override 2026-11-04, MODEL.md "Engine card"): from 2026-11-05 the NBA card's
pool is the NBA engine's picks only (signal `engine`, research name `nba-engine`), chosen by the
same rule: the top 5 by EV among picks still Live at 6:15 AM ET, labelled TAKE (1 unit) when the
EV is >= 1.5x the engine's threshold, else LEAN (0.5 unit). A short card is chosen once (the page
and the push say "N picks today — the engine found no other value"); with nothing Live, the
first later poll with a Live pick selects it (owner 2026-11-05, as Card v1 always did). A pick
found after selection stays off the card (tracked on the Research tab).
The NFL card is unchanged.

Two official NBA cards (owner decision 2026-11-05, MODEL.md "Two official NBA cards";
config/cards.yaml): "Next Gen" (the engine card above, state `card:nba:<day>`, ledger/card.csv)
and "OG" (Card v1: top 5 by EV among the F and C-NBA picks still Live, M18 labels; state
`shadow:nba:<day>`, ledger/shadow_card.csv; its record continues from Card v1). Both are chosen at
6:15 AM ET by the "When" rule below and pushed together by the morning update; F and C-NBA alerts
are recorded, never pushed one by one. `active: false` in config/cards.yaml retires a card.

When (owner correction 2026-11-05, both NBA cards): if lines exist at 6:15 for at least one of
today's games, the card is selected then from what is available, even short or empty ("No picks
today — the engine found no value"); picks found later are tracked on the Research tab and never
join it. Only if no lines exist for any of today's games does a later poll select it, at the
first one where lines exist. Cutoff: with still no lines 60 minutes before the day's first tip,
the card is published empty ("No lines posted in time"). Chosen once per day in every case
(`decide`).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from betdata.alerts.db import alerts, get_state, set_state
from betdata.alerts.labels import (
    ENGINE,
    LEAN,
    SIGNALS,
    STAKE,
    TAKE,
    available,
    engine_gate,
    gate_applies,
    label,
)
from betdata.alerts.rules import EASTERN, decimal

SIZE = 5
AT = time(6, 15)  # Eastern: the morning update (owner 2026-11-05; was 8:50 until then)
FIRST_DAY = date(2026, 10, 31)
SPORTS = ("nba", "nfl")
ENGINE_START = date(2026, 11, 5)  # the first engine card (owner override 2026-11-04)
SHADOW = "card-v1-shadow"  # the stored source tag of an OG card (kept: the ledger is append-only)
V1_SIGNALS = ("value", "snipe")  # OG's (Card v1's) NBA pool: C-NBA and F (nba15/nbap15-based)


def _active(card: str) -> bool:
    from betdata import cards as C

    return C.active(card)


def engine_day(sport: str, day: date) -> bool:
    """Is the sport's card on this day the engine card, Next Gen (NBA from ENGINE_START)?"""
    return sport == "nba" and day >= ENGINE_START


def shadow_day(sport: str, day: date) -> bool:
    """Is an OG card (Card v1's rule, stored as `shadow:`) chosen for the sport on the day? Not
    on its excluded switch-over day (config/cards.yaml)."""
    from betdata import cards as C

    return (engine_day(sport, day) and _active("og")
            and day.isoformat() not in C.load()[C.OG].exclude_days)  # fmt: skip


def shadow_signal(a) -> bool:
    """An NBA F or C-NBA pick from ENGINE_START: recorded, never pushed one by one (the OG card
    is pushed by the morning update)."""
    day = a.tip.astimezone(EASTERN).date() if a.tip is not None else date.max
    return a.sport == "nba" and a.signal in V1_SIGNALS and engine_day("nba", day)


def signals_for(sport: str, day: date) -> tuple[str, ...]:
    """The signals whose picks are the sport's card pool (Next Gen's for the NBA)."""
    return (ENGINE,) if engine_day(sport, day) else SIGNALS


def today_signals(sport: str, day: date) -> tuple[str, ...]:
    """The signals listed in the sport's Today section: both NBA cards' pools."""
    return (ENGINE, *V1_SIGNALS) if engine_day(sport, day) else SIGNALS


NO_LINES = "No lines posted in time"
LINES_CUTOFF = timedelta(minutes=60)  # before the day's first tip


def lines_exist(p, main, sport: str, day: date, now: datetime) -> bool:
    """Is any DK/FD (or other archived) line stored, by `now`, for one of the sport's games on
    the Eastern day?"""
    from betdata.alerts.db import quotes
    from betdata.alerts.grade import QUOTE_SOURCES
    from betdata.db.schema import games, sports

    lo, hi = day_bounds(day)
    gids = list(main.execute(select(games.c.game_id)
                             .join(sports, sports.c.sport_id == games.c.sport_id)
                             .where(sports.c.key == sport, games.c.start_time_utc >= lo,
                                    games.c.start_time_utc < hi,
                                    games.c.status.not_in(("postponed", "cancelled"))))
                .scalars())  # fmt: skip
    if not gids:
        return False
    return p.execute(select(quotes.c.quote_id).where(
        quotes.c.game_id.in_(gids), quotes.c.sport == sport, quotes.c.polled_at <= now,
        quotes.c.source.in_(QUOTE_SOURCES), quotes.c.price.is_not(None)).limit(1)).first() \
        is not None  # fmt: skip


def decide(p, m, sport: str, day: date, now: datetime, first: datetime, signals=None,
           lines_fn=None) -> dict | None:
    """The card to store now, or None to wait (module docstring "When"): lines for any of
    today's games -> select from what is available (even empty); no lines -> wait, until 60
    minutes before the first tip, then an empty card marked no_lines."""
    has = (lines_fn or lines_exist)(p, m, sport, day, now)
    if not has:
        if now < first - LINES_CUTOFF:
            return None
        card = {"sport": sport, "day": day.isoformat(), "selected_at": now.isoformat(),
                "picks": [], "live": 0, "no_lines": True}
        card["source"] = SHADOW if signals is not None else (
            "engine" if engine_day(sport, day) else None)
        if card["source"] is None:
            del card["source"]
        return card
    return choose(p, sport, day, now, signals, m)


def decide_og(p, m, sport: str, day: date, now: datetime, first: datetime,
              lines_fn=None) -> dict | None:  # fmt: skip
    """OG's own selection path (owner 2026-11-11, MODEL.md "OG Ranking v2"): the same "When" rule
    as `decide` (lines -> select now, even empty; no lines -> wait, until 60 minutes before the
    first tip, then an empty card marked no_lines), with OG's ranking (`choose_og`). Next Gen's
    `decide` / `choose` are not used here, so OG's rules can change without touching Next Gen."""
    has = (lines_fn or lines_exist)(p, m, sport, day, now)
    if not has:
        if now < first - LINES_CUTOFF:
            return None
        return {"sport": sport, "day": day.isoformat(), "selected_at": now.isoformat(),
                "picks": [], "live": 0, "no_lines": True, "source": SHADOW}
    return choose_og(p, sport, day, now, m)


def choose_og(p, sport: str, day: date, now: datetime, m=None) -> dict:
    """OG's card at `now`: before Ranking v2, Card v1's rule (top SIZE Live F / C-NBA picks by
    EV, after rules v2); from its first day (Nov 12), Ranking v2 (betdata/og_ranking.py)."""
    from betdata import og_ranking as OR

    if OR.applies(day):
        return OR.card(p, sport, day, now, m)
    return choose(p, sport, day, now, V1_SIGNALS, m)


def card_note(card: dict) -> str:
    """The page and push note of a short or empty NBA card ("" for a full one)."""
    if card.get("no_lines"):
        return NO_LINES
    n = len(card.get("picks", []))
    if n >= SIZE:
        return ""
    return short_note(n, "OG" if card.get("source") == SHADOW else "the engine")


def short_note(n: int, who: str = "the engine") -> str:
    """A card's note when it has fewer than SIZE picks."""
    if n == 0:
        return f"No picks today — {who} found no value"
    return f"{n} pick{'s' if n != 1 else ''} today — {who} found no other value"


def key(sport: str, day: date) -> str:
    return f"card:{sport}:{day.isoformat()}"


def shadow_key(sport: str, day: date) -> str:
    return f"shadow:{sport}:{day.isoformat()}"


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), EASTERN)
    return start, start + timedelta(days=1)


def select_at(day: date) -> datetime:
    """The card's selection time on the Eastern day: 6:15 AM ET."""
    return datetime.combine(day, AT, EASTERN)


def first_tip(main, sport: str, day: date) -> datetime | None:
    """The sport's first scheduled start on the Eastern day (main database)."""
    from betdata.alerts.grade import utc
    from betdata.db.schema import games, sports

    lo, hi = day_bounds(day)
    t = main.execute(select(games.c.start_time_utc)
                     .join(sports, sports.c.sport_id == games.c.sport_id)
                     .where(sports.c.key == sport, games.c.start_time_utc >= lo,
                            games.c.start_time_utc < hi,
                            games.c.status.not_in(("postponed", "cancelled")))
                     .order_by(games.c.start_time_utc)).first()  # fmt: skip
    return None if t is None else utc(t[0])


def ev(a) -> float | None:
    """Expected value per unit staked at the alerted price."""
    if a.fair_prob is None or a.price is None:
        return None
    return a.fair_prob * decimal(a.price) - 1


def _ns(r) -> SimpleNamespace:
    from betdata.alerts.grade import utc

    d = dict(r._mapping)
    d["tip"] = None if d["tip"] is None else utc(d["tip"])
    d["details"] = d.get("details") or {}
    return SimpleNamespace(**d)


def day_picks(p, sport: str, day: date, signals=None) -> list[SimpleNamespace]:
    """The sport's card-pool picks for games on the Eastern day (M18 signals; the NBA engine's
    from ENGINE_START; `signals` overrides, e.g. V1_SIGNALS for the shadow card)."""
    lo, hi = day_bounds(day)
    return [_ns(r) for r in p.execute(select(alerts).where(
        alerts.c.sport == sport, alerts.c.signal.in_(signals or signals_for(sport, day)),
        alerts.c.tip >= lo, alerts.c.tip < hi)
        .order_by(alerts.c.created_at, alerts.c.alert_id))]  # fmt: skip


def choose(p, sport: str, day: date, now: datetime, signals=None, m=None) -> dict:
    """The card at `now` (pure on the poller database): top SIZE live picks by EV among the
    eligible ones (rules v2 from Nov 10, betdata/eligibility.py; `m`: the main database, for
    the teammates-out rule)."""
    from betdata import eligibility as EL
    from betdata.alerts.grade import utc

    pool, filtered = [], {}
    for a in day_picks(p, sport, day, signals):
        if utc(a.created_at) > now or a.tip is None or a.tip <= now:
            continue
        why = EL.alert_reason(m, a)  # rules v2: what is eligible for each card
        if why is not None:
            filtered[a.dedup_key] = why
            continue
        x = ev(a)
        if x is None or available(p, a, now) is not True:
            continue
        # Next Gen: the long-shot gate (owner 2026-11-09) from its first card, Nov 10, also
        # for engine picks created before the gate shipped
        if a.signal == ENGINE and gate_applies(a) and not engine_gate(a):
            continue
        pool.append((x, a))
    pool.sort(key=lambda t: -t[0])
    picks = []
    for x, a in pool[:SIZE]:
        lab = label(p, a, now) or {}
        lb = TAKE if lab.get("label") == TAKE else LEAN
        picks.append({"dedup_key": a.dedup_key, "label": lb, "stake": STAKE[lb],
                      "ev": round(x, 4), "edge": lab.get("edge"),
                      "threshold": lab.get("threshold"), "edge_source": lab.get("edge_source"),
                      "pinnacle_sides": lab.get("pinnacle_sides")})  # fmt: skip
    out = {"sport": sport, "day": day.isoformat(), "selected_at": now.isoformat(),
           "picks": picks, "live": len(pool)}
    if filtered:
        out["filtered"] = filtered
    if signals is not None:
        out["source"] = SHADOW
    elif engine_day(sport, day):
        out["source"] = "engine"
    return out


def in_ledger(sport: str, day: date) -> bool:
    """A rebuilt poller database must not choose a day's card again."""
    from betdata.ledger import ledger_dir, read_rows

    return any(r["sport"] == sport and r["card_date"] == day.isoformat()
               for r in read_rows(ledger_dir() / "card.csv"))  # fmt: skip


def shadow_in_ledger(sport: str, day: date) -> bool:
    from betdata.ledger import ledger_dir, read_rows

    return any(r["sport"] == sport and r["card_date"] == day.isoformat()
               for r in read_rows(ledger_dir() / "shadow_card.csv"))  # fmt: skip


def run_shadow(pdb, main, now: datetime, tip_fn=first_tip, lines_fn=None) -> list[dict]:
    """Choose the shadow Card v1 when due (once), by the same rule as the engine card (`decide`):
    top 5 by EV among F/C-NBA picks still Live. Never pushed."""
    day = now.astimezone(EASTERN).date()
    out = []
    for sport in SPORTS:
        if not shadow_day(sport, day) or now < select_at(day):
            continue
        with pdb.connect() as p:
            if get_state(p, shadow_key(sport, day)) is not None:
                continue
        if shadow_in_ledger(sport, day):
            continue
        with main.connect() as m:
            ft = tip_fn(m, sport, day)
            if ft is None:
                continue
            with pdb.begin() as p:
                card = decide_og(p, m, sport, day, now, ft, lines_fn)  # OG's own path
                if card is None:  # no lines for any of today's games yet: wait
                    continue
                set_state(p, shadow_key(sport, day), card, now)
                from betdata import og_ranking as OR

                if OR.applies(day):  # the OG Top 3 shadow: same ranking, same moment, 3 picks
                    set_state(p, OR.top3_key(sport, day), OR.top3_of(card), now)
                from betdata import action as AC

                if AC.applies(day):  # the Action card (owner 2026-11-12): not official
                    set_state(p, AC.key(day), AC.choose(p, day, now, card, m), now)
        out.append(card)
    return out


def run(pdb, main, now: datetime, tip_fn=first_tip, lines_fn=None) -> list[dict]:
    """Choose each sport's card when due (once): from 6:15 AM ET on a day the sport has games,
    at the first poll with any Live pick. Returns the cards chosen now."""
    day = now.astimezone(EASTERN).date()
    if day < FIRST_DAY:
        return []
    out = []
    for sport in SPORTS:
        with pdb.connect() as p:
            if get_state(p, key(sport, day)) is not None:
                continue
        if in_ledger(sport, day):
            continue
        with main.connect() as m:
            ft = tip_fn(m, sport, day)
            if ft is None or now < select_at(day):  # no games today, or not 6:15 yet
                continue
            if engine_day(sport, day) and not _active("nextgen"):  # Next Gen retired
                continue
            with pdb.begin() as p:
                if engine_day(sport, day):  # the engine card: `decide` (owner 2026-11-05)
                    card = decide(p, m, sport, day, now, ft, None, lines_fn)
                    if card is None:  # no lines for any of today's games yet: wait
                        continue
                else:  # NFL (and Card v1 after a revert): nothing Live yet -> the next poll
                    card = choose(p, sport, day, now, None, m)
                    if not card["picks"]:
                        continue
                set_state(p, key(sport, day), card, now)
        out.append(card)
    return out


def message(p, card: dict, now: datetime) -> tuple[str, str]:
    """The ntfy push for one sport's card: (title, body), one line per pick."""
    from betdata.alerts.notify import BOOKS, _fmt_price, selection

    og = card.get("source") == SHADOW
    by_key = {a.dedup_key: a for a in day_picks(p, card["sport"],
                                                datetime.fromisoformat(card["day"]).date(),
                                                V1_SIGNALS if og else None)}
    lines = []
    for x in card["picks"]:
        a = by_key.get(x["dedup_key"])
        if a is None:
            continue
        when = a.tip.astimezone(EASTERN).strftime("%-I:%M %p ET")
        lines.append(f"{a.game_label} {when}: {selection(a)} {_fmt_price(a.price)} at "
                     f"{BOOKS.get(a.book, a.book)}, {x['label']} {x['stake']:g}u")
    if card.get("source") in ("engine", SHADOW) and card_note(card):
        lines.append(card_note(card))
    if card.get("source") == "engine":  # Spread Picks ride in Next Gen's push (owner 2026-11-10)
        try:
            from betdata import spread as SP

            d = datetime.fromisoformat(card["day"]).date()
            if get_state(p, SP.key(d)) is not None:  # chosen with this Next Gen card
                lines += SP.push_lines(p, None, d)
        except Exception:  # noqa: BLE001 - never breaks the card push
            pass
    day = datetime.fromisoformat(card["day"]).strftime("%a %b %-d")
    who = card["sport"].upper()
    if card.get("source") in ("engine", SHADOW):
        from betdata import cards as C

        who += " " + C.name("og" if og else "nextgen")
    return f"Tonight's card - {who} - {day}", "\n".join(lines)


def push(pdb, card: dict, now: datetime, topic: str | None, client=None) -> str:
    """Push one sport's card to ntfy and record the outcome on the stored card."""
    import httpx

    from betdata.alerts.notify import NTFY, PAGE

    with pdb.connect() as p:
        title, body = message(p, card, now)
    if not topic:
        status = "no NTFY_TOPIC"
    else:
        try:
            r = (client or httpx.Client(timeout=15)).post(
                NTFY + topic, content=body.encode(),
                headers={"Title": title, "Click": PAGE, "Tags": "clipboard"})
            status = f"http {r.status_code}"
        except Exception as e:  # noqa: BLE001 - a failed push never loses the card
            status = f"error {type(e).__name__}"
    with pdb.begin() as p:
        d = datetime.fromisoformat(card["day"]).date()
        k = shadow_key(card["sport"], d) if card.get("source") == SHADOW else key(card["sport"], d)
        st = get_state(p, k) or card
        set_state(p, k, {**st, "push": status}, now)
    return status


def last_tip(main, sport: str, day: date) -> datetime | None:
    """The sport's last scheduled start on the Eastern day (main database)."""
    from betdata.alerts.grade import utc
    from betdata.db.schema import games, sports

    lo, hi = day_bounds(day)
    t = main.execute(select(games.c.start_time_utc)
                     .join(sports, sports.c.sport_id == games.c.sport_id)
                     .where(sports.c.key == sport, games.c.start_time_utc >= lo,
                            games.c.start_time_utc < hi,
                            games.c.status.not_in(("postponed", "cancelled")))
                     .order_by(games.c.start_time_utc.desc())).first()  # fmt: skip
    return None if t is None else utc(t[0])


def pending(pdb, main, start: datetime, end: datetime, tip_fn=first_tip,
            last_fn=last_tip) -> bool:  # fmt: skip
    """Keeps the poller awake for the card: on a day with games, from 6:15 AM ET (due by `end`)
    until the card is chosen or the day's last game starts. (REVIEW.md L-4: a check for 6:15
    falling inside [start, end] missed it when the loop's clock read 6:15:00.009.)"""
    day = start.astimezone(EASTERN).date()
    if day < FIRST_DAY or end < select_at(day):
        return False
    for sport in SPORTS:
        with pdb.connect() as p:
            card_done = get_state(p, key(sport, day)) is not None or in_ledger(sport, day)
            shadow_done = (not shadow_day(sport, day)
                           or get_state(p, shadow_key(sport, day)) is not None
                           or shadow_in_ledger(sport, day))  # fmt: skip
        if card_done and shadow_done:
            continue
        with main.connect() as m:
            if tip_fn(m, sport, day) is None:
                continue
            last = last_fn(m, sport, day)
        if last is not None and start < last:
            return True
    return False
