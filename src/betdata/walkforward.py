"""Walk-forward evaluation of OG card rules (owner decision 2026-11-12; report only, nothing here
changes a card). `betdata ledger walkforward`, `reports/nba/walkforward.md`.

For each OG card day D (card_days.csv: Card v1 days to Nov 4, OG days from Nov 6; Nov 5, the
switch-over day, is excluded as in the OG record) at D's stored selection time T:
- a rule sees only picks **created before T** (F / C-NBA picks for D's games, from the ledger);
- any parameter (a calibration fit) is fitted only on picks **graded before T**;
- the pool is one pick per player per game; today's rules: Rules v2 (assists only, no unders on
  5.5+, no unders with 2+ key teammates out where the ledger recorded it), then Ranking v2's
  price and median rules (the median as `review_pick.proj_median` stores it).
The ledger holds no quotes, so the pool cannot apply the Live check (the price still offered at
T); the runner replay in reports/nba/og_ranking_v2.md does. Split mode (`cutoff`): parameters
fitted only on picks graded before the cutoff, scored only on days from the cutoff on.

Rules: `actual` (the OG card as chosen: EV-ranked to Nov 11, Ranking v2 from Nov 12), `v2top5`,
`v2top3`, `v2cal` (Ranking v2 on F's calibrated probability, refitted walk-forward: identical to
v2top5 by construction), `segcal` (research only: a separate Platt fit per segment, assists over
vs under x line 1.5 / 2.5 / 3.5 / 4.5+, fitted walk-forward; the global fit where a segment has
fewer than SEG_MIN picks).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

RULES = ("actual", "v2top5", "v2top3", "v2cal", "segcal")
FIRST_DAY = "2026-10-31"
EXCLUDE = ("2026-11-05",)
STAKE = 0.5
SEG_MIN = 40
REPORT = Path("reports") / "nba" / "walkforward.md"
OG_SIGNALS = ("snipe", "value")


def _et_date(iso: str) -> str:
    from betdata.alerts.rules import EASTERN

    return datetime.fromisoformat(iso).astimezone(EASTERN).date().isoformat()


@dataclass
class Data:
    picks: list  # every OG-signal NBA pick joined with result, annotations and median
    days: dict  # card date -> selection time (datetime)
    actual: dict  # card date -> the actual OG card rows
    graded: list = field(default_factory=list)  # F picks (win/loss) with graded_at, for fits


def load(out: Path) -> Data:
    from betdata import cards as C
    from betdata import official as O
    from betdata.ledger import counted, joined, read_rows

    rp = {r["pick_id"]: r for r in read_rows(out / "review_pick.csv")}
    picks = []
    for r in counted(joined(out)):
        if r["sport"] != "nba" or r["signal"] not in OG_SIGNALS:
            continue
        picks.append({**r, "proj_median": rp.get(r["pick_id"], {}).get("proj_median", "")})
    days = {}
    for r in read_rows(out / "card_days.csv"):
        d = r["card_date"]
        if r["sport"] != "nba" or d < FIRST_DAY or d in EXCLUDE or not r.get("selected_at"):
            continue
        if (d <= "2026-11-04" and r["kind"] == "v1") or (d >= "2026-11-06"
                                                          and r["kind"] == "shadow"):
            days[d] = datetime.fromisoformat(r["selected_at"])
    actual: dict = {}
    for r in C.view(O.dataset(out), C.OG):
        if r["day"] in days:
            actual.setdefault(r["day"], []).append(r)
    graded = [r for r in picks if r["signal_name"] == "F" and r.get("result") in ("win", "loss")
              and r.get("graded_at") and r.get("fair_prob")]  # fmt: skip
    return Data(picks, dict(sorted(days.items())), actual, graded)


def _ns(r: dict) -> SimpleNamespace:
    def f(x):
        return None if x in ("", None) else float(x)

    med = f(r.get("proj_median"))
    return SimpleNamespace(
        dedup_key=r["pick_id"], game_id=r["game_id"], player_name=r["player"] or None,
        market=r["market"], side=r["side"], line=f(r["line"]), price=int(r["odds"]),
        fair_prob=f(r["fair_prob"]), created_at=datetime.fromisoformat(r["created_at"]),
        details={} if med is None else {"median": med}, row=r)  # fmt: skip


def _rules_v2(r: dict) -> str | None:
    """Rules v2 for OG on the ledger: teammates-out is known only where the ledger recorded it
    (the `filter` annotation, from Nov 10); elsewhere that rule cannot apply."""
    from betdata import eligibility as EL

    tm = 2 if r.get("filter") == EL.TEAMMATES else None
    return EL.reason("og", r["market"], r["side"], float(r["line"]) if r["line"] else None, tm)


def pool(data: Data, day: str, t: datetime) -> tuple[list, int, dict]:
    """(candidates passing Rules v2 and Ranking v2's price / median rules, raw count, why-not)
    for the card day: picks for that day's games created before `t` (never later)."""
    from betdata import og_ranking as OR

    raw = [r for r in data.picks if r.get("tip") and _et_date(r["tip"]) == day
           and datetime.fromisoformat(r["created_at"]) < t]  # fmt: skip
    out, why = [], {}
    for r in raw:
        w = _rules_v2(r)
        a = _ns(r)
        w = w or OR.reason(a)
        if w:
            why[w] = why.get(w, 0) + 1
            continue
        out.append(a)
    return out, len(raw), why


def global_fit(data: Data, before: datetime):
    """F's Platt fit on F picks graded before `before`, one per player per game (None: < 200
    picks or not increasing, as the live rule)."""
    from betdata import f_calibration as FC
    from betdata.f_verdict import dedup

    rows = dedup([r for r in data.graded if datetime.fromisoformat(r["graded_at"]) < before])
    f, _ = FC.fit_points([float(r["fair_prob"]) for r in rows],
                         [int(r["result"] == "win") for r in rows], before.date())  # fmt: skip
    return f


def segment(r) -> str:
    side = r.side if hasattr(r, "side") else r["side"]
    line = r.line if hasattr(r, "line") else float(r["line"] or 0)
    b = "4.5+" if line is None or line >= 4.5 else f"{line:g}"
    return f"{side} {b}"


def segment_fits(data: Data, before: datetime) -> dict:
    """Research only: a Platt fit per segment (graded before `before`), the global fit where a
    segment has fewer than SEG_MIN picks or is not increasing."""
    from betdata.calibration import Fit, fit
    from betdata.f_verdict import dedup

    rows = dedup([r for r in data.graded if datetime.fromisoformat(r["graded_at"]) < before
                  and r["market"] == "assists"])  # fmt: skip
    out = {"_global": global_fit(data, before)}
    by: dict = {}
    for r in rows:
        by.setdefault(segment(r), []).append(r)
    for k, rs in by.items():
        if len(rs) < SEG_MIN:
            continue
        a, b = fit([float(r["fair_prob"]) for r in rs], [int(r["result"] == "win") for r in rs])
        if a > 0:
            out[k] = Fit(before.date().isoformat(), len(rs), a, b)
    return out


def _pick_row(a, day: str) -> dict:
    r = a.row
    return {**r, "day": day, "card_stake": str(STAKE)}


def run(data: Data, rule: str, cutoff: date | None = None) -> dict:
    """{day: [rows]} chosen by `rule` on each card day (from `cutoff` when given), plus counts."""
    from datetime import time

    from betdata import og_ranking as OR
    from betdata.alerts.rules import EASTERN
    from betdata.f_calibration import calibrate

    chosen, counts = {}, {}
    frozen_at = None if cutoff is None else datetime.combine(cutoff, time(0), EASTERN)
    for day, t in data.days.items():
        if cutoff is not None and day < cutoff.isoformat():
            continue
        if rule == "actual":
            chosen[day] = data.actual.get(day, [])
            continue
        cands, n_raw, _ = pool(data, day, t)
        fit_t = t if frozen_at is None else min(t, frozen_at)
        if rule in ("v2top5", "v2top3"):
            ranked, _ = OR.order(cands, None)
        elif rule == "v2cal":
            ranked, _ = OR.order(cands, global_fit(data, fit_t))
        elif rule == "segcal":
            fits = segment_fits(data, fit_t)

            def key(a, fits=fits):
                from betdata.alerts.rules import decimal

                f = fits.get(segment(a), fits["_global"])
                return (-calibrate(a.fair_prob, f), decimal(a.price), a.created_at)

            best: dict = {}
            for a in sorted(cands, key=key):
                best.setdefault((a.game_id, (a.player_name or "").lower()), a)
            ranked = sorted(best.values(), key=key)
        else:
            raise ValueError(f"unknown rule {rule!r}; one of {RULES}")
        size = 3 if rule == "v2top3" else 5
        chosen[day] = [_pick_row(a, day) for a in ranked[:size]]
        counts[day] = (n_raw, len(cands), len(ranked))
    return {"days": chosen, "counts": counts}


def metrics(res: dict):
    """The og_review side-by-side metrics over the chosen rows (graded only)."""
    from betdata import og_review as OGR

    rows = [r for rs in res["days"].values() for r in rs]
    days = list(res["days"])
    return OGR.side("", rows, days)


def table(named: list[tuple[str, dict]]) -> list[str]:
    def roi(s):
        return "–" if s.rec.roi is None else f"{100 * s.rec.roi:+.1f}%"

    def pct(s):
        d = s.rec.wins + s.rec.losses
        return "–" if not d else f"{100 * s.rec.wins / d:.1f}%"

    def odds(s):
        o = s.rec.avg_odds
        return "–" if o is None else (f"+{o}" if o > 0 else str(o))

    out = ["| Rule | Record | Win % | Units (0.5u) | ROI | Avg odds | Picks/day | Worst 3 days |",
           "|---|---|---|---|---|---|---|---|"]
    for name, res in named:
        s = metrics(res)
        ppd = "–" if not s.days else f"{s.picks / s.days:.1f}"
        out.append(f"| {name} | {s.rec.wlp} | {pct(s)} | {s.rec.units:+.2f} | {roi(s)} | "
                   f"{odds(s)} | {ppd} | {s.worst[1]}, {s.worst[2]:+.2f}u ({s.worst[0]}) |")
    return out


def same_picks(a: dict, b: dict) -> tuple[int, int]:
    """(days where two rules chose the same picks in the same order, days compared)."""
    ds = [d for d in a["days"] if d in b["days"]]
    return (sum([r["pick_id"] for r in a["days"][d]] == [r["pick_id"] for r in b["days"][d]]
                for d in ds), len(ds))


NAMES = {"actual": "(i) Actual OG card", "v2top5": "(ii) Ranking v2 top 5",
         "v2top3": "(iii) Ranking v2 top 3",
         "v2cal": "(iv) Ranking v2 on calibrated p (walk-forward fits)",
         "segcal": "(v) Research: per-segment calibration (walk-forward)"}


def report(out: Path, now: datetime, cutoff: date | None = None) -> str:
    data = load(out)
    full = {k: run(data, k) for k in RULES}
    cut = cutoff or date(2026, 11, 12)
    split = {k: run(data, k, cut) for k in RULES}
    raw = sum(c[0] for c in full["v2top5"]["counts"].values())
    pre = sum(c[1] for c in full["v2top5"]["counts"].values())
    dd = sum(c[2] for c in full["v2top5"]["counts"].values())
    s4, n4 = same_picks(full["v2top5"], full["v2cal"])
    s5, n5 = same_picks(full["v2top5"], full["segcal"])
    md = [f"# Walk-forward: OG card rules ({now.date().isoformat()})", "",
          "Report only (owner 2026-11-12): nothing here changes a card. Each card day is "
          "evaluated at its stored selection time with picks created before it; calibration "
          "fits use only picks graded before it. One pick per player per game. Units at 0.5u; "
          "graded picks only.", "",
          f"Card days: {len(data.days)} ({min(data.days, default='–')} to "
          f"{max(data.days, default='–')}; Nov 5 excluded). Pool, all days: {raw} raw "
          f"candidate picks created before selection → {pre} after Rules v2 and Ranking v2's "
          f"price / median rules → {dd} after one pick per player per game.", "",
          "**In-sample vs out-of-sample, plainly:** Ranking v2's rules (price range, median "
          "side, ranking by probability) were chosen by the owner after looking at Oct 31 - "
          "Nov 10, so rules (ii)-(iv) on those days are **in-sample** for the rule design even "
          "though no pick or grade from the future is used. Only days from Nov 12 on are "
          "out-of-sample for the rules. The calibration fits in (iv) and (v) are walk-forward, "
          "i.e. out-of-sample for their parameters on every day.", "",
          "## All card days (rules (ii)-(iv) in-sample for the rule design)", "",
          *table([(NAMES[k], full[k]) for k in RULES]), "",
          f"- (iv) vs (ii): the same picks in the same order on {s4} of {n4} days "
          "(calibration is increasing, so it cannot change the order).",
          f"- (v) vs (ii): the same picks in the same order on {s5} of {n5} days.", "",
          f"## Split: parameters frozen before {cut.isoformat()}, scored from it (out-of-sample)",
          "", *table([(NAMES[k], split[k]) for k in RULES]), "",
          f"Days scored: {len(split['actual']['days'])}"
          + (" — too few to read anything yet." if len(split["actual"]["days"]) < 10 else "."),
          "", "## Differences from the runner replay (reports/nba/og_ranking_v2.md)", "",
          "This tool reads the ledger only. It cannot apply the Live check (no quotes in the "
          "ledger), so its pool is every pick created before selection — compare it with the "
          "replay's \"every pick created before selection\" rows (top 5 22-16, top 3 17-8, "
          "Oct 31 - Nov 10). The teammates-out rule applies only where the ledger recorded it "
          "(from Nov 10); before that the runner replay used the injury report known at "
          "selection and removed some unders this tool keeps (on the first run: Anthony Black "
          "and Desmond Bane on Nov 1 and Nov 7, Coby White and Naz Reid on Nov 9), which is "
          "the whole difference in (ii) on those days. This tool also includes Nov 11 (pending "
          "at the first run). The median is `review_pick.proj_median`, the field the live "
          "rule reads.", ""]
    return "\n".join(md) + "\n"


def write(root: Path, now: datetime, cutoff: date | None = None) -> Path:
    path = root / REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report(root / "ledger", now, cutoff))
    return path
