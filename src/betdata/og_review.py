"""OG Ranking v2 review (owner decision 2026-11-11, MODEL.md "OG Ranking v2"): official OG
(Ranking v2 picks) vs the OG Top 3 shadow, in `reports/nba/og_ranking_v2.md` (rewritten every
morning) and the weekly report; the review milestone at Nov 26 or 50 graded official Ranking v2
picks; and the hindsight backtest that motivated the change. Report only: nothing switches.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from betdata.og_ranking import REPORT_PATH, STAKE, start

MILESTONE = "og-ranking-v2"
REVIEW_N = 50
REVIEW_DATE = date(2026, 11, 26)
BACKTEST = Path("reports") / "nba" / "og_ranking_v2_backtest.json"
OWNER_REPLAY = {"actual": "20-25", "top5": "23-20", "top3": "16-11"}


def _f(x) -> float | None:
    return None if x in ("", None) else float(x)


def official_rows(rows: list[dict]) -> list[dict]:
    """Official OG Ranking v2 picks (OG card rows from the Ranking v2 date)."""
    from betdata import cards as C

    return [r for r in C.view(rows, C.OG) if r["day"] >= start().isoformat()]


def rebound_overs(rows: list[dict]) -> list[dict]:
    """Official OG rebound overs under Rules v3 (owner 2026-11-12): OG card rows from Nov 13."""
    from betdata import cards as C
    from betdata.eligibility import RULES_V3_START

    return [r for r in C.view(rows, C.OG) if r["day"] >= RULES_V3_START.isoformat()
            and r.get("market") == "rebounds" and r.get("side") == "over"]  # fmt: skip


def rebound_overs_line(rows: list[dict]) -> str:
    from betdata import official as O

    rec = O.record(rebound_overs(rows), O.official_stake)
    d = rec.wins + rec.losses
    win = "–" if not d else f"{100 * rec.wins / d:.1f}%"
    return (f"- Rebound overs (official, Rules v3 from Nov 13): {rec.wlp}, win {win}, "
            f"{rec.units:+.2f}u at 0.5u ({rec.graded} graded; graded picks only)")


def side_split(rows: list[dict]) -> list[str]:
    """The Rules v3 review (owner 2026-11-12): rebound overs on Action, and official OG and Action
    by market and side (rebounds, assists, threes) from Nov 13; graded picks only."""
    from betdata import cards as C
    from betdata import official as O
    from betdata.eligibility import RULES_V3_START

    since = RULES_V3_START.isoformat()
    cards = (("Official OG (0.5u)", C.OG), ("Action (0.25u, not official)", C.ACTION))
    view = {k: [r for r in C.view(rows, k) if r["day"] >= since] for _n, k in cards}
    ac = O.record([r for r in view[C.ACTION] if r.get("market") == "rebounds"
                   and r.get("side") == "over"], O.official_stake)  # fmt: skip
    out = [f"- Rebound overs (Action, from Nov 13): {ac.wlp}, {ac.units:+.2f}u at 0.25u "
           f"({ac.graded} graded)", "",
           "| Market · side | " + " | ".join(n for n, _k in cards) + " |",
           "|---|" + "---|" * len(cards)]
    for mk in ("rebounds", "assists", "threes"):
        for sd in ("over", "under"):
            cells = []
            for _n, k in cards:
                rec = O.record([r for r in view[k] if r.get("market") == mk
                                and r.get("side") == sd], O.official_stake)  # fmt: skip
                d = rec.wins + rec.losses
                cells.append(f"{rec.wlp}, {rec.units:+.2f}u" + (
                    f" ({100 * rec.wins / d:.0f}%)" if d else ""))
            out.append(f"| {mk} · {sd} | " + " | ".join(cells) + " |")
    return out


def top3_rows(out: Path) -> list[dict]:
    """The Top 3 shadow's picks with their results, each at its own 0.5u stake."""
    from betdata.ledger import read_rows

    res = {r["pick_id"]: r for r in read_rows(out / "results.csv")}
    picks = {r["pick_id"]: r for r in read_rows(out / "picks.csv")}
    rows = []
    for r in read_rows(out / "og_top3.csv"):
        x, p = res.get(r["pick_id"], {}), picks.get(r["pick_id"], {})
        rows.append({**p, "pick_id": r["pick_id"], "day": r["card_date"],
                     "card_stake": r["og3_stake"], "odds": p.get("odds") or r["price"],
                     "result": x.get("result", ""), "profit": x.get("profit", ""),
                     "clv_consensus": x.get("clv_consensus", "")})  # fmt: skip
    return rows


def threes_shadow_rows(out: Path) -> list[dict]:
    """The OG + threes shadow's picks (owner 2026-11-12) with their results, 0.5u each."""
    from betdata.ledger import read_rows

    res = {r["pick_id"]: r for r in read_rows(out / "results.csv")}
    picks = {r["pick_id"]: r for r in read_rows(out / "picks.csv")}
    rows = []
    for r in read_rows(out / "og_threes_shadow.csv"):
        x, p = res.get(r["pick_id"], {}), picks.get(r["pick_id"], {})
        rows.append({**p, "pick_id": r["pick_id"], "day": r["card_date"],
                     "market": r.get("market") or p.get("market", ""),
                     "card_stake": r["stake"], "odds": p.get("odds") or r["price"],
                     "result": x.get("result", ""), "profit": x.get("profit", ""),
                     "clv_consensus": x.get("clv_consensus", "")})  # fmt: skip
    return rows


EARN_IN_N = 30  # graded threes picks on the shadow before the bar can be met


def earn_in(rows: list[dict], out: Path) -> list[str]:
    """The threes earn-in bar (owner 2026-11-12; report only): over the shadow's days, the OG +
    threes shadow beats official OG on win %, AND its threes picks are .500+ over >= 30
    graded. Nothing switches; the owner decides."""
    from betdata import official as O

    sh = threes_shadow_rows(out)
    days = set(card_days(out, "ogthrees"))
    if not days:
        return ["No OG + threes shadow card yet (it starts with the Nov 13 morning card)."]
    off = [r for r in official_rows(rows) if r["day"] in days]
    a, b = O.record(off, O.official_stake), O.record(sh, O.official_stake)
    t = O.record([r for r in sh if r.get("market") == "threes"], O.official_stake)

    def pct(rec):
        d = rec.wins + rec.losses
        return None if not d else rec.wins / d

    pa, pb, pt = pct(a), pct(b), pct(t)
    head = (f"- Same days ({len(days)}): official OG {a.wlp} ({_p(pa)}) vs OG + threes shadow "
            f"{b.wlp} ({_p(pb)}); threes on the shadow {t.wlp} ({_p(pt)}, {t.graded} graded).")
    if t.graded < EARN_IN_N:
        verdict = (f"**Not enough yet:** {t.graded} of {EARN_IN_N} graded threes picks on the "
                   "shadow.")
    else:
        beat = pa is not None and pb is not None and pb > pa
        ok = pt is not None and pt >= 0.5
        verdict = ("**Threes met the bar**" if beat and ok else "**Threes did not meet the bar**"
                   ) + (f" (shadow {'beat' if beat else 'did not beat'} official OG on win %; "
                        f"threes {'at' if ok else 'below'} .500).")  # fmt: skip
    return [head, f"- {verdict} Nothing switches automatically; the owner decides."]


def threes_replay(root: Path) -> list[str]:
    """The OG + threes shadow replayed Oct 31 - Nov 12 (runner, real databases): labelled
    hindsight; no made-threes picks existed then, so it is OG Ranking v2's card."""
    import json

    from betdata import official as O
    from betdata.action_review import ADDS_REPLAY

    path = root / ADDS_REPLAY
    if not path.exists():
        return ["Replay data not found."]
    data = json.loads(path.read_text())
    rows = [{"day": d, "result": p.get("result", ""), "profit": p.get("profit", ""),
             "card_stake": "0.5", "odds": str(p["price"]), "clv_consensus": "",
             "market": p.get("market", "")}
            for d, x in sorted(data.items()) for p in x["shadow"]]  # fmt: skip
    threes = sum(x.get("threes_picks_that_day", 0) for x in data.values())
    same = sum([p["dedup_key"] for p in x["shadow"]] == x["og_v2"] for x in data.values())
    rec = O.record(rows, O.official_stake)
    return ["**Hindsight replay, not live results.** Each OG card day Oct 31 - Nov 12 (Nov 5 "
            "excluded) at its stored selection time. No made-threes picks existed before Nov 13 "
            f"(threes picks found: {threes}), so the shadow equals the OG Ranking v2 card on "
            f"{same} of {len(data)} days.", "",
            f"- Record: {rec.wlp}, {rec.units:+.2f}u at 0.5u ({rec.graded} graded)", ""]


def _p(x) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def card_days(out: Path, kind: str) -> list[str]:
    from betdata.ledger import read_rows

    return sorted({r["card_date"] for r in read_rows(out / "card_days.csv")
                   if r["sport"] == "nba" and r["kind"] == kind
                   and r["card_date"] >= start().isoformat()})  # fmt: skip


@dataclass
class Side:
    name: str
    rec: object
    days: int
    picks: int
    worst: tuple  # (days, W-L, units)


def worst_stretch(rows: list[dict], days: list[str]) -> tuple:
    """The worst 3 consecutive card days by units (fewer days: all of them)."""
    from betdata import official as O

    if not days:
        return ("–", "0-0", 0.0)
    span = 3 if len(days) >= 3 else len(days)
    best = None
    for i in range(len(days) - span + 1):
        ds = days[i:i + span]
        rec = O.record([r for r in rows if r["day"] in ds], O.official_stake)
        if best is None or rec.units < best[2]:
            best = (f"{ds[0]} to {ds[-1]}", f"{rec.wins}-{rec.losses}", round(rec.units, 2))
    return best


def side(name: str, rows: list[dict], days: list[str]) -> Side:
    from betdata import official as O

    rec = O.record(rows, O.official_stake)
    return Side(name, rec, len(days), sum(1 for r in rows if r["day"] in days),
                worst_stretch(rows, days))


def comparison(rows: list[dict], out: Path) -> tuple[Side, Side]:
    return (side("OG official (Ranking v2, top 5)", official_rows(rows), card_days(out, "shadow")),
            side("OG Top 3 (shadow)", top3_rows(out), card_days(out, "og3")))


def _pct(rec) -> str:
    d = rec.wins + rec.losses
    return "–" if not d else f"{100 * rec.wins / d:.1f}%"


def _odds(rec) -> str:
    o = rec.avg_odds
    return "–" if o is None else (f"+{o}" if o > 0 else str(o))


def table(a: Side, b: Side) -> list[str]:
    def roi(s):
        return "–" if s.rec.roi is None else f"{100 * s.rec.roi:+.1f}%"

    def ppd(s):
        return "–" if not s.days else f"{s.picks / s.days:.1f}"

    def worst(s):
        return f"{s.worst[1]}, {s.worst[2]:+.2f}u ({s.worst[0]})"

    return ["| | " + a.name + " | " + b.name + " |", "|---|---|---|",
            f"| Record (W-L-P) | {a.rec.wlp} | {b.rec.wlp} |",
            f"| Win % | {_pct(a.rec)} | {_pct(b.rec)} |",
            f"| Units (0.5u) | {a.rec.units:+.2f} | {b.rec.units:+.2f} |",
            f"| ROI | {roi(a)} | {roi(b)} |",
            f"| Average odds | {_odds(a.rec)} | {_odds(b.rec)} |",
            f"| Picks per day | {ppd(a)} ({a.picks} over {a.days} days) | "
            f"{ppd(b)} ({b.picks} over {b.days} days) |",
            f"| Worst 3-day stretch | {worst(a)} | {worst(b)} |"]  # fmt: skip


# --- the hindsight backtest (report only) ------------------------------------------------------


def backtest(root: Path, rows: list[dict]) -> list[str]:
    """The replay that motivated Ranking v2 (MODEL.md): the actual EV-ranked OG card vs Ranking
    v2 top 5 vs top 3, Oct 31 - Nov 10 (Nov 5 excluded, as in the OG record)."""
    from betdata import cards as C
    from betdata import official as O

    path = root / BACKTEST
    if not path.exists():
        return ["Backtest data not found."]
    data = json.loads(path.read_text())
    actual = [r for r in C.view(rows, C.OG) if "2026-10-31" <= r["day"] <= "2026-11-10"]
    a = O.record(actual, O.official_stake)

    def rec(pool: str, n: int):
        w = lo = pu = 0
        u = 0.0
        per = []
        for d, x in sorted(data[pool].items()):
            ps = x["picks"][:n]
            dw = sum(p["result"] == "win" for p in ps)
            dl = sum(p["result"] == "loss" for p in ps)
            w, lo, pu = w + dw, lo + dl, pu + sum(p["result"] == "push" for p in ps)
            u += sum(STAKE * float(p["profit"] or 0) for p in ps
                     if p["result"] in ("win", "loss", "push"))  # fmt: skip
            per.append(f"{d[5:]}: {len(ps)} ({dw}-{dl})")
        return f"{w}-{lo}" + (f"-{pu}" if pu else ""), round(u, 2), per

    out = ["**Hindsight replay, not live results.** Oct 31 - Nov 10 (Nov 5 excluded, as in the "
           "OG record), each day replayed on the runner at its stored card-selection time with "
           "today's rules (Rules v2 on every day) and only picks created before that selection; "
           "units at 0.5u.", "",
           "| Card | Record | Units (0.5u) | Owner's replay | Picks per day |",
           "|---|---|---|---|---|",
           f"| Actual OG card (EV-ranked) | {a.wins}-{a.losses} | {a.units:+.2f} | "
           f"{OWNER_REPLAY['actual']} | 5 a day |"]
    for pool, label in (("live", "Live at selection (the live rule)"),
                        ("created", "every pick created before selection")):
        for n, k in ((5, "top5"), (3, "top3")):
            r, u, per = rec(pool, n)
            out.append(f"| Ranking v2 top {n}, {label} | {r} | {u:+.2f} | {OWNER_REPLAY[k]} | "
                       f"{', '.join(per)} |")
    out += ["", "Why the numbers differ from the owner's 23-20 / 16-11: this replay applies "
            "every live rule at the stored selection moment (Rules v2 including the "
            "teammates-out rule from the injury report known then, the Live check on stored "
            "quotes, one pick per player, the probability / price tie-breaks), counts "
            "graded picks only (voids and not-yet-graded picks are out), and uses the stored "
            "median fields (`median`, else `mean_after`). Short days are days with fewer than "
            "5 eligible picks; they are never filled."]
    return out


# --- report only: each pick's label and EV on F's calibrated probability (owner 2026-11-12) ---


def calibrated_rows(out: Path) -> list[dict]:
    """Every official OG Ranking v2 pick with its raw and calibrated probability, EV and label
    (the label's existing rule re-read with the calibrated edge). Report only: the card's labels
    and stakes stay on the existing logic."""
    from betdata.alerts.rules import decimal
    from betdata.f_calibration import calibrate, current
    from betdata.ledger import read_rows

    picks = {r["pick_id"]: r for r in read_rows(out / "picks.csv")}
    res = {r["pick_id"]: r for r in read_rows(out / "results.csv")}
    rows = []
    for r in read_rows(out / "shadow_card.csv"):
        if r["shadow"] != "yes" or r["card_date"] < start().isoformat():
            continue
        pk = picks.get(r["pick_id"])
        if pk is None or not pk.get("fair_prob"):
            continue
        f = current(out, date.fromisoformat(r["card_date"]))
        p = float(pk["fair_prob"])
        c = calibrate(p, f)
        dec = decimal(int(pk["odds"]))
        edge, thr = _f(r.get("edge")), _f(r.get("threshold"))
        take = None
        if edge is not None and thr is not None:
            ec = edge - (p - c)
            take = (ec >= 1.5 * thr - 1e-9
                    and (r.get("edge_source") == "news" or r.get("pinnacle_sides") == "true"))
        rows.append({"day": r["card_date"], "pick": f"{pk['player']} {pk['side']} "
                     f"{pk['line']} {pk['market']}", "odds": int(pk["odds"]), "p": p, "p_cal": c,
                     "ev": p * dec - 1, "ev_cal": c * dec - 1, "label": r["shadow_label"],
                     "label_cal": "–" if take is None else ("TAKE" if take else "LEAN"),
                     "fit": "none" if f is None else f.fit_date,
                     "result": res.get(r["pick_id"], {}).get("result", "")})  # fmt: skip
    return rows


def calibrated_table(out: Path) -> list[str]:
    rows = calibrated_rows(out)
    if not rows:
        return ["No official Ranking v2 picks yet."]
    return ["| Day | Pick | Odds | Model p | Calibrated p | EV | EV calibrated | Label | "
            "Label calibrated | F fit | Result |", "|---|---|---|---|---|---|---|---|---|---|---|",
            *[f"| {r['day']} | {r['pick']} | {r['odds']:+d} | {100 * r['p']:.1f}% | "
              f"{100 * r['p_cal']:.1f}% | {100 * r['ev']:+.1f}% | {100 * r['ev_cal']:+.1f}% | "
              f"{r['label']} | {r['label_cal']} | {r['fit']} | {r['result'] or 'pending'} |"
              for r in rows]]  # fmt: skip


# --- the report and the milestone --------------------------------------------------------------


def report(root: Path, now: datetime) -> str:
    from betdata import official as O

    out = root / "ledger"
    rows = O.dataset(out)
    a, b = comparison(rows, out)
    g = official_rows(rows)
    n = O.record(g, O.official_stake).graded
    md = [f"# OG Ranking v2 ({now.date().isoformat()})", "",
          f"Owner decision 2026-11-11 (MODEL.md \"OG Ranking v2\"): from {start():%b %-d} OG "
          "ranks its pool by the model's win probability, -150 to +130, median on the bet's "
          "side, one pick per player, up to 5 at 0.5u. The OG Top 3 is a shadow: never shown, "
          "pushed or counted officially. Nothing switches automatically; the owner decides.", "",
          f"Review: at {REVIEW_DATE:%b %-d} or {REVIEW_N} graded official Ranking v2 picks, "
          f"whichever first ({n} graded so far).", "",
          f"## Official OG vs OG Top 3 (since {start():%b %-d}, graded only)", "",
          *table(a, b), "", rebound_overs_line(rows), "",
          "## Rules v3 review: rebound overs and the side split (from Nov 13, graded only)", "",
          "Owner decision 2026-11-12 (MODEL.md \"Rules v3\"): rebound overs are back on the "
          "official card; the guardrail alerts at 7-13 or worse over the last 20 official "
          "rebound overs. Nothing switches automatically.", "", *side_split(rows), "",
          "## OG + threes shadow (owner 2026-11-12; never official)", "",
          "OG Ranking v2's rules with made threes eligible, chosen at 6:15 AM ET from Nov 13; "
          "graded only, 0.5u.", "",
          *table(*_threes_sides(rows, out)), "",
          "### Threes earn-in bar (report only)", "", *earn_in(rows, out), "",
          "### OG + threes shadow, hindsight replay (Oct 31 - Nov 12)", "",
          *threes_replay(root),
          "## Calibrated probability (report only)", "",
          "F's weekly calibration (MODEL.md \"F calibration\") on each official pick: the "
          "card ranks on it (the same picks and order as the raw probability); its labels and "
          "stakes stay on the existing logic. The page shows the raw probability.", "",
          *calibrated_table(out), "",
          "## Backtest (hindsight replay)", "", *backtest(root, rows), ""]  # fmt: skip
    return "\n".join(md) + "\n"


def _threes_sides(rows: list[dict], out: Path) -> tuple[Side, Side]:
    days = card_days(out, "ogthrees")
    sh = threes_shadow_rows(out)
    off = [r for r in official_rows(rows) if r["day"] in set(days)]
    a = side("OG official, same days", off, days)
    b = side("OG + threes shadow", sh, days)
    t = side("its threes picks only", [r for r in sh if r.get("market") == "threes"], days)
    b.name = f"OG + threes shadow (threes only: {t.rec.wlp}, {t.rec.units:+.2f}u)"
    return a, b


def write(root: Path, now: datetime) -> Path:
    path = root / REPORT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report(root, now))
    return path


def milestone(out: Path, now: datetime) -> list[dict]:
    """The review, once: at REVIEW_DATE (Eastern) or REVIEW_N graded official picks."""
    from betdata import official as O
    from betdata.alerts.rules import EASTERN
    from betdata.ledger import _append_milestones, _iso, read_rows

    if any(r["signal_name"] == MILESTONE for r in read_rows(out / "milestones.csv")):
        return []
    n = O.record(official_rows(O.dataset(out)), O.official_stake).graded
    today = now.astimezone(EASTERN).date() if now is not None else date.min
    if n < REVIEW_N and today < REVIEW_DATE:
        return []
    new = [{"signal_name": MILESTONE, "graded": str(min(n, REVIEW_N) if n >= REVIEW_N else n),
            "reached_at": _iso(now)}]
    _append_milestones(out / "milestones.csv", new)
    return new


def ntfy_line(out: Path) -> str:
    from betdata import official as O

    rec = O.record(official_rows(O.dataset(out)), O.official_stake)
    return (f"OG Ranking v2 review ready: official {rec.wlp}, {rec.units:+.2f}u over "
            f"{rec.graded} graded picks — see {REPORT_PATH} (vs the Top 3 shadow; nothing "
            "switches, owner decides)")
