"""OG Ranking v2 (owner decision 2026-11-11, MODEL.md "OG Ranking v2"): OG's official card is
ranked by the OG model's win probability instead of EV, from the Nov 12 morning card.

The pool is unchanged (F and C-NBA picks still Live at selection, after Rules v2). Then:
- price: American odds from -150 to +130 inclusive at the pick's book;
- median: the model's projected median on the bet's side of the line (over: median > line;
  under: median < line; equal is rejected), from the alert's structured fields as
  review_pick.proj_median stores it (`median`, else `mean_after`); none -> not eligible;
- one pick per player per game: the highest model probability;
- rank by `fair_prob`, highest first; ties: shorter price (lower decimal odds), then earlier
  created_at; up to 5 (the official card) or 3 (the OG Top 3 shadow); 0.5u each.
Every candidate a rule removes keeps its reason (`og_rank` annotations in the ledger).
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

PRICE_MIN, PRICE_MAX = -150, 130
SIZE = 5
TOP3 = 3
STAKE = 0.5
PRICE = "og: price outside -150..+130"
NO_MEDIAN = "og: no median"
MEDIAN_SIDE = "og: median not on the bet's side"
DUPLICATE = "og: duplicate player"
OUTSIDE = "og: outside top 5"
NO_PROB = "og: no model probability"
REASONS = (PRICE, NO_MEDIAN, MEDIAN_SIDE, DUPLICATE, OUTSIDE, NO_PROB)


@lru_cache(maxsize=1)
def start() -> date:
    """The first card day of Ranking v2 (config/cards.yaml og.ranking_v2_since)."""
    import yaml

    from betdata import cards as C

    raw = yaml.safe_load(C.PATH.read_text())["nba"]["cards"]["og"]
    return date.fromisoformat(str(raw.get("ranking_v2_since", "2026-11-12")))


def applies(day: date) -> bool:
    return day >= start()


def median_of(a) -> float | None:
    """The model's projected median, as review_pick.proj_median stores it."""
    d = a.details or {}
    v = d.get("median", d.get("mean_after"))
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def reason(a) -> str | None:
    """The first price / median rule the pick fails, or None."""
    if a.fair_prob is None:
        return NO_PROB
    if a.price is None or not PRICE_MIN <= int(a.price) <= PRICE_MAX:
        return PRICE
    if a.side not in ("over", "under"):
        return NO_MEDIAN  # a game market has no player median
    med = median_of(a)
    if med is None or a.line is None:
        return NO_MEDIAN
    if a.side == "over" and not med > a.line:
        return MEDIAN_SIDE
    if a.side == "under" and not med < a.line:
        return MEDIAN_SIDE
    return None


def _key(a, fit=None):
    """The ranking key: the calibrated model probability (F's fit, owner 2026-11-12; increasing,
    so the same order as the raw probability), then the shorter price, then the earlier pick."""
    from betdata.alerts.grade import utc
    from betdata.alerts.rules import decimal
    from betdata.f_calibration import calibrate

    return (-calibrate(a.fair_prob, fit), decimal(int(a.price)), utc(a.created_at))


def order(cands: list, fit=None) -> tuple[list, dict]:
    """(ranked, rejected): one pick per player per game (the best by the ranking key), all
    sorted by the ranking key; the others rejected as duplicates."""
    from functools import partial

    key = partial(_key, fit=fit)
    best: dict = {}
    rejected = {}
    for a in sorted(cands, key=key):
        k = (a.game_id, (a.player_name or "").lower())
        if k in best:
            rejected[a.dedup_key] = DUPLICATE
            continue
        best[k] = a
    return sorted(best.values(), key=key), rejected


def pick_entry(p, a, now, label_fn, fit=None) -> dict:
    from betdata.alerts.card import ev
    from betdata.alerts.labels import LEAN, TAKE

    lab = label_fn(p, a, now) or {}
    lb = TAKE if lab.get("label") == TAKE else LEAN
    x = ev(a)
    return {"dedup_key": a.dedup_key, "label": lb, "stake": STAKE,
            "ev": None if x is None else round(x, 4), "edge": lab.get("edge"),
            "threshold": lab.get("threshold"), "edge_source": lab.get("edge_source"),
            "pinnacle_sides": lab.get("pinnacle_sides"),
            "fair_prob": round(float(a.fair_prob), 4), "price": int(a.price),
            "fair_prob_cal": round(_cal(a.fair_prob, fit), 4),
            "median": median_of(a), "line": a.line}  # fmt: skip


def _cal(x, fit):
    from betdata.f_calibration import calibrate

    return calibrate(x, fit)


def f_fit(day: date):
    """F's calibration fit in force for the day (None before the first fit)."""
    try:
        from betdata.f_calibration import current
        from betdata.ledger import ledger_dir

        return current(ledger_dir(), day)
    except Exception:  # noqa: BLE001 - no fit: the raw probability (the same order)
        return None


def ranked_pool(p, sport: str, day: date, now, m=None,
                fit="current") -> tuple[list, dict, dict, int]:  # fmt: skip
    """(ranked, filtered, rejected, live) at `now`: OG's pool (F / C-NBA picks for the day's
    games, created by `now`, not started, after Rules v2 -> `filtered`, still Live), then the
    Ranking v2 rules (-> `rejected`), one per player, in ranking order."""
    from betdata import eligibility as EL
    from betdata.alerts.card import V1_SIGNALS, day_picks
    from betdata.alerts.grade import utc
    from betdata.alerts.labels import available

    pool, filtered, rejected = [], {}, {}
    for a in day_picks(p, sport, day, V1_SIGNALS):
        if utc(a.created_at) > now or a.tip is None or a.tip <= now:
            continue
        why = EL.alert_reason(m, a)  # Rules v2 first, exactly as before
        if why is not None:
            filtered[a.dedup_key] = why
            continue
        if available(p, a, now) is not True:
            continue
        r = reason(a)
        if r is not None:
            rejected[a.dedup_key] = r
            continue
        pool.append(a)
    if fit == "current":
        fit = f_fit(day)
    ranked, dup = order(pool, fit)
    rejected.update(dup)
    return ranked, filtered, rejected, len(pool)


def card(p, sport: str, day: date, now, m=None, size: int = SIZE) -> dict:
    """The Ranking v2 card (official: size 5; the Top 3 shadow: size 3)."""
    from betdata.alerts.card import SHADOW
    from betdata.alerts.labels import label

    fit = f_fit(day)
    ranked, filtered, rejected, live = ranked_pool(p, sport, day, now, m, fit)
    picks = [pick_entry(p, a, now, label, fit) for a in ranked[:size]]
    for a in ranked[size:]:
        rejected[a.dedup_key] = OUTSIDE if size == SIZE else f"og: outside top {size}"
    out = {"sport": sport, "day": day.isoformat(), "selected_at": now.isoformat(),
           "picks": picks, "live": live, "ranking": "v2", "source": SHADOW}
    if filtered:
        out["filtered"] = filtered
    if rejected:
        out["og_rejected"] = rejected
    if fit is not None:
        out["f_fit"] = fit.fit_date
    return out


# --- the ledger: each rejected candidate's reason, once --------------------------------------


def annotation_rows(p, out, now) -> int:
    """One `og_rank` annotation per candidate a Ranking v2 rule removed on a chosen OG card
    (official, from the stored `shadow:` state), next to the Rules v2 `filter` annotations."""
    from sqlalchemy import select

    from betdata.alerts.db import state
    from betdata.ledger import ANNOTATION_FIELDS, _iso, append_rows, pick_id

    rows = []
    for st in p.execute(select(state).where(state.c.key.like("shadow:%"))):
        c = st.value or {}
        for k, why in (c.get("og_rejected") or {}).items():
            pid = pick_id(k)
            rows.append({"key": f"{pid}:og_rank", "pick_id": pid, "field": "og_rank",
                         "value": why, "noted_at": _iso(now),
                         "note": "OG Ranking v2 (owner 2026-11-11): not on the OG card; "
                                 "still graded"})  # fmt: skip
    return append_rows(out / "annotations.csv", ANNOTATION_FIELDS, rows, key="key")


def filters_table(rows: list[dict]) -> list[dict]:
    """The Filters table's OG Ranking v2 rows: per reason, the removed picks' graded record
    (flat 1 unit), win rate vs break-even, and the OG card's record over the same period."""
    from betdata import cards as C
    from betdata import official as O

    since = start().isoformat()
    og = [r for r in rows if r["sport"] == "nba" and r.get("og_rank")]
    card = O.record([r for r in C.view(rows, C.OG) if r["day"] >= since], O.official_stake)
    out = []
    for why in REASONS:
        removed = [r for r in og if r["og_rank"] == why]
        if not removed:
            continue
        rec, g = O.record(removed), O.goal(removed)
        d = rec.wins + rec.losses
        out.append({"card": C.name(C.OG), "reason": why, "removed": len(removed),
                    "graded": rec.graded, "wlp": rec.wlp, "units": round(rec.units, 2),
                    "win": None if not d else round(rec.wins / d, 4),
                    "needed": None if g.break_even is None else round(g.break_even, 4),
                    "card_wlp": card.wlp, "card_units": round(card.units, 2),
                    "card_graded": card.graded,
                    "status": "Ranking v2 (owner 2026-11-11); reviewed with the card"})
    return out


# --- the OG Top 3 shadow (owner 2026-11-11): never shown, pushed or counted officially ------

TOP3_SOURCE = "og-top3-shadow"
TOP3_FIELDS = ("pick_id", "sport", "card_date", "og3", "og3_label", "og3_stake", "selected_at",
               "rank", "fair_prob", "price", "median", "line")  # fmt: skip


def top3_key(sport: str, day) -> str:
    return f"og3:{sport}:{day if isinstance(day, str) else day.isoformat()}"


def top3_of(card: dict) -> dict:
    """The Top 3 shadow from OG's Ranking v2 card: the same pool, rules and moment, the first 3
    of the same ranking (a card of 5 is that ranking's first 5)."""
    out = {k: card[k] for k in ("sport", "day", "selected_at") if k in card}
    out.update(picks=list(card.get("picks", []))[:TOP3], source=TOP3_SOURCE, ranking="v2")
    if card.get("no_lines"):
        out["no_lines"] = True
    return out


def top3_rows(p, out, now) -> int:
    """Write-once rows of ledger/og_top3.csv (one per Top 3 pick) and a card-day row (kind
    `og3`, also when empty)."""
    from sqlalchemy import select

    from betdata.alerts.db import state
    from betdata.ledger import CARD_DAY_FIELDS, _num, append_rows, pick_id

    rows, days = [], []
    for st in p.execute(select(state).where(state.c.key.like("og3:%"))):
        c = st.value or {}
        if not c.get("day"):
            continue
        for i, x in enumerate(c.get("picks", []), 1):
            rows.append({"pick_id": pick_id(x["dedup_key"]), "sport": c["sport"],
                         "card_date": c["day"], "og3": "yes", "og3_label": x.get("label", ""),
                         "og3_stake": _num(x.get("stake", STAKE)),
                         "selected_at": c.get("selected_at", ""), "rank": str(i),
                         "fair_prob": _num(x.get("fair_prob")), "price": x.get("price", ""),
                         "median": _num(x.get("median")), "line": _num(x.get("line"), 2)})
        days.append({"key": f"{c['sport']}:{c['day']}:og3", "sport": c["sport"],
                     "card_date": c["day"], "kind": "og3",
                     "selected_at": c.get("selected_at", ""),
                     "picks": str(len(c.get("picks", [])))})  # fmt: skip
    n = append_rows(out / "og_top3.csv", TOP3_FIELDS, rows)
    append_rows(out / "card_days.csv", CARD_DAY_FIELDS, days, key="key")
    return n


# --- the slump alert (owner 2026-11-11): a review trigger, never a switch --------------------

SLUMP_N = 15  # the last 15 graded official Ranking v2 picks...
SLUMP_DAYS = 3  # ...across the most recent 3 card days
SLUMP_WINS_MAX = 3  # 3-12 or worse
REPORT_PATH = "reports/nba/og_ranking_v2.md"


SHORT_N = 9  # owner 2026-11-12: also the last 9 decided official Ranking v2 picks...
SHORT_WINS_MAX = 2  # ...at 2-7 or worse (any number of days: fires on short cards too)


def _decided(rows: list[dict]) -> list[dict]:
    """Official OG Ranking v2 picks with a win or a loss (pushes, voids and pending excluded),
    in order."""
    from betdata import cards as C

    v2 = [r for r in C.view(rows, C.OG) if r["day"] >= start().isoformat()
          and r.get("result") in ("win", "loss")]  # fmt: skip
    return sorted(v2, key=lambda r: (r["day"], r.get("tip", ""), r.get("created_at", "")))


def slump_rule(rows: list[dict]) -> tuple[str, int, int] | None:
    """(which, wins, losses) of the first rule that fires, else None:
    - "3 days": the last 15 decided picks over the most recent 3 card days are 3-12 or worse;
    - "9 picks": the last 9 decided picks (any number of days) are 2-7 or worse."""
    v2 = _decided(rows)
    days = sorted({r["day"] for r in v2})[-SLUMP_DAYS:]
    last = [r for r in v2 if r["day"] in days][-SLUMP_N:]
    if len(last) == SLUMP_N:
        w = sum(r["result"] == "win" for r in last)
        if w <= SLUMP_WINS_MAX:
            return "3 days", w, SLUMP_N - w
    last = v2[-SHORT_N:]
    if len(last) == SHORT_N:
        w = sum(r["result"] == "win" for r in last)
        if w <= SHORT_WINS_MAX:
            return "9 picks", w, SHORT_N - w
    return None


def slump(rows: list[dict]) -> tuple[int, int] | None:
    """(wins, losses) when either slump rule fires (`slump_rule`), else None."""
    s = slump_rule(rows)
    return None if s is None else (s[1], s[2])


def slump_line(w: int, lo: int, which: str = "3 days") -> str:
    span = "the last 3 days" if which == "3 days" else f"the last {SHORT_N} graded picks"
    return f"OG review: {w}-{lo} over {span} — check {REPORT_PATH}"


def slump_check(pdb, rows: list[dict], now, topic: str | None, client=None) -> str | None:
    """After the morning grading: one ntfy line when `slump` fires, at most once a day (poller
    state `og-slump:<day>`). Changes nothing else."""
    import httpx

    from betdata.alerts.db import get_state, set_state
    from betdata.alerts.notify import NTFY, PAGE
    from betdata.alerts.rules import EASTERN

    day = now.astimezone(EASTERN).date().isoformat()
    key = f"og-slump:{day}"
    with pdb.connect() as p:
        if get_state(p, key) is not None:
            return None
    s = slump_rule(rows)
    if s is None:
        return None
    line = slump_line(s[1], s[2], s[0])
    if not topic:
        status = "no NTFY_TOPIC"
    else:
        try:
            r = (client or httpx.Client(timeout=15)).post(
                NTFY + topic, content=line.encode(),
                headers={"Title": "OG review", "Click": PAGE, "Tags": "warning"})
            status = f"http {r.status_code}"
        except Exception as e:  # noqa: BLE001 - never breaks the morning
            status = f"error {type(e).__name__}"
    with pdb.begin() as p:
        set_state(p, key, {"line": line, "status": status}, now)
    return status
