"""F's pre-registered re-judgment (BACKLOG.md M20: "Judge F again on its next 200 picks (from
2026-11-04) under the M20 rule"; MODEL.md M20 Part A, the three checks that replaced the M18
Part H gate). Run as written, once (owner 2026-11-12):

- Window: F's first 200 graded picks (win / loss / push; voids out) created on or after
  2026-11-04 00:00 ET, in grading order (the order results.csv received them), m16-legacy and
  duplicate-annotated rows excluded (`ledger.counted`).
- The three checks exactly as `betdata/checks.py` implements MODEL.md: calibration (gap's 95%
  range reaches 0 or below AND ECE <= 0.05 over the MODEL.md bins), Brier vs the no-vig consensus
  close (paired 95% range below 0), EV at close (95% range above 0). Pushes are left out of the
  checks, as MODEL.md says.
- Verdict: "works" only if all three pass; else "does not work" with the failed checks.
- Not the verdict (labelled so): every F pick graded since Nov 4, and the 200-pick window with one
  pick per player per game (highest model probability kept).
The verdict changes no rule, pick or grade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SIGNAL = "F"
START_ET = "2026-11-04"  # 00:00 ET
WINDOW = 200
MILESTONE = "F:verdict-nov4"
REPORT = Path("reports") / "nba" / "f_verdict.md"
GRADED = ("win", "loss", "push")


def _start_utc() -> datetime:
    from datetime import time

    from betdata.alerts.rules import EASTERN

    return datetime.combine(datetime.fromisoformat(START_ET).date(), time(0), EASTERN)


def graded_since(out: Path) -> list[dict]:
    """Every F pick created on/after Nov 4 00:00 ET with a graded result (win/loss/push), in
    grading order (results.csv row order: each result is appended once, when final)."""
    from betdata.ledger import counted, joined, read_rows

    order = {r["pick_id"]: i for i, r in enumerate(read_rows(out / "results.csv"))}
    t0 = _start_utc()
    rows = [r for r in counted(joined(out))
            if r["signal_name"] == SIGNAL and r.get("result") in GRADED
            and datetime.fromisoformat(r["created_at"]) >= t0]  # fmt: skip
    return sorted(rows, key=lambda r: order[r["pick_id"]])


def window(out: Path) -> list[dict]:
    return graded_since(out)[:WINDOW]


def dedup(rows: list[dict]) -> list[dict]:
    """One pick per player per game: the highest model probability (ties: the earlier)."""
    best: dict = {}
    for r in rows:
        k = (r["game_id"], (r["player"] or "").lower())
        p = float(r["fair_prob"] or 0)
        if k not in best or p > float(best[k]["fair_prob"] or 0):
            best[k] = r
    keep = {id(r) for r in best.values()}
    return [r for r in rows if id(r) in keep]


@dataclass
class Result:
    rows: list
    checks: object  # betdata.checks.Checks

    @property
    def failed(self) -> list[str]:
        c = self.checks
        return [x.name for x in (c.calibration, c.brier, c.ev) if x.passed is not True]

    @property
    def verdict(self) -> str:
        return "works" if not self.failed else "does not work"


def judge(rows: list[dict]) -> Result:
    from betdata.checks import picks_of, three_checks

    return Result(rows, three_checks(picks_of(rows)))


def reliability5(rows: list[dict]) -> list[tuple]:
    """(bucket, n, mean predicted, win rate) in 5-point buckets of the model probability."""
    out = []
    ps = [(float(r["fair_prob"]), r["result"] == "win") for r in rows
          if r.get("result") in ("win", "loss") and r.get("fair_prob")]  # fmt: skip
    for lo in range(30, 100, 5):
        b = [(p, y) for p, y in ps if lo / 100 <= p < (lo + 5) / 100]
        if b:
            out.append((f"{lo}-{lo + 5}%", len(b), sum(p for p, _ in b) / len(b),
                        sum(y for _, y in b) / len(b)))
    low = [(p, y) for p, y in ps if p < 0.30]
    if low:
        out.insert(0, ("< 30%", len(low), sum(p for p, _ in low) / len(low),
                       sum(y for _, y in low) / len(low)))
    return out


def _r(c, pct: bool = True) -> str:
    def f(x):
        return "–" if x is None else (f"{100 * x:+.2f}" if pct else f"{x:+.4f}")

    rng = "" if c.low is None else f" (95% {f(c.low)} to {f(c.high)})"
    return f"{f(c.value)}{rng}"


def section(title: str, res: Result, note: str = "") -> list[str]:
    from betdata import official as O

    c = res.checks
    rec = O.record(res.rows)
    cal = c.calibration
    gap = (f"gap {cal.note.split('gap ')[-1]}" if "gap" in (cal.note or "") else "")
    return [f"## {title}", "", *([note, ""] if note else []),
            f"- Picks: {len(res.rows)} graded ({rec.wlp}; {c.n} win/loss in the checks), "
            f"{rec.units:+.2f}u at 1u flat, ROI "
            + ("–" if rec.roi is None else f"{100 * rec.roi:+.1f}%"),
            f"- Mean model probability {100 * _mean_p(res.rows):.1f}% vs win rate "
            f"{100 * _win(res.rows):.1f}%",
            f"- 1. Calibration: ECE {('–' if cal.value is None else f'{cal.value:.3f}')} "
            f"(max 0.050); {gap}; gap 95% range {_pp(cal.low)} to {_pp(cal.high)} pp "
            f"→ **{_pf(cal.passed)}**",
            f"- 2. Brier vs the no-vig close: {_r(c.brier, pct=False)} over {c.brier.n} picks "
            f"with a close → **{_pf(c.brier.passed)}**",
            f"- 3. EV at close: {_r(c.ev)}% over {c.ev.n} picks with a close → "
            f"**{_pf(c.ev.passed)}**",
            f"- Result: **{res.verdict}**"
            + ("" if not res.failed else f" (failed: {', '.join(res.failed)})"), ""]


def _pp(x) -> str:
    return "–" if x is None else f"{100 * x:+.2f}"


def _pf(x) -> str:
    return "pass" if x is True else "fail" if x is False else "not computable"


def _mean_p(rows):
    ps = [float(r["fair_prob"]) for r in rows
          if r.get("result") in ("win", "loss") and r.get("fair_prob")]  # fmt: skip
    return sum(ps) / len(ps) if ps else 0.0


def _win(rows):
    ys = [r["result"] == "win" for r in rows if r.get("result") in ("win", "loss")]
    return sum(ys) / len(ys) if ys else 0.0


def report(out: Path, now: datetime) -> tuple[str, Result]:
    w = window(out)
    res = judge(w)
    allr = judge(graded_since(out))
    dd = judge(dedup(w))
    rel = reliability5(w)
    first, last = (w[0], w[-1]) if w else ({}, {})
    md = [f"# F verdict: the next 200 picks from Nov 4 ({now.date().isoformat()})", "",
          "Pre-registered in BACKLOG.md (M20): \"Judge F again on its next 200 picks (from "
          "2026-11-04) under the M20 rule.\" Run as written (MODEL.md M20 Part A, the three "
          "checks that replaced the M18 Part H gate; `betdata/checks.py`). The verdict changes "
          "no rule, pick or grade.", "",
          f"**Verdict: F {res.verdict}**"
          + ("" if not res.failed else f" — failed: {', '.join(res.failed)}") + ".", "",
          f"Window: F's first {WINDOW} graded picks (win/loss/push; voids, m16-legacy and "
          f"duplicates excluded) created on or after {START_ET} 00:00 ET, in grading order: "
          f"{len(w)} picks, created {first.get('created_at', '–')} to "
          f"{last.get('created_at', '–')}. Pushes are left out of the checks (MODEL.md). "
          "Closing no-vig = book_prob + clv_consensus (the consensus close).", "",
          *section("The verdict window (the pre-registered test)", res),
          "### Reliability (5-point buckets of the model probability; display only, the ECE "
          "uses MODEL.md's bins)", "", "| Bucket | n | Mean predicted | Win rate |",
          "|---|---|---|---|",
          *[f"| {b} | {n} | {100 * p:.1f}% | {100 * y:.1f}% |" for b, n, p, y in rel], "",
          *section("NOT the verdict: every F pick graded since Nov 4", allr,
                   f"{len(allr.rows)} graded picks (the window is the first {WINDOW})."),
          *section("NOT the verdict: the window, one pick per player per game", dd,
                   f"{len(w)} raw picks → {len(dd.rows)} after keeping the highest model "
                   "probability per player and game (F often alerts the same player at "
                   "several books or lines; those results are correlated).")]  # fmt: skip
    return "\n".join(md) + "\n", res


def write(root: Path, now: datetime) -> tuple[Path, Result]:
    md, res = report(root / "ledger", now)
    path = root / REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md)
    return path, res


def milestone(out: Path, now: datetime) -> list[dict]:
    """Once the window is complete: one milestone row (signal_name F:verdict-nov4)."""
    from betdata.ledger import _append_milestones, _iso, read_rows

    if any(r["signal_name"] == MILESTONE for r in read_rows(out / "milestones.csv")):
        return []
    w = window(out)
    if len(w) < WINDOW:
        return []
    new = [{"signal_name": MILESTONE, "graded": str(WINDOW), "reached_at": _iso(now)}]
    _append_milestones(out / "milestones.csv", new)
    return new


def ntfy_line(out: Path) -> str:
    return (f"F verdict (next 200 from Nov 4): {judge(window(out)).verdict} — see "
            f"{REPORT.as_posix()}")
