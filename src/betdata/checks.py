"""M20 Part A: the three-check success test, over the ledger (MODEL.md "M20: Part A").

A signal **works** only if all three pass over >= 200 graded picks (pushes and voids left out):
1. **Calibration:** reliability table and ECE; the overall gap (mean p - win rate) must not be
   significantly above 0 (its 95% range reaches 0 or below) and ECE <= 0.05.
2. **Closer than the close:** Brier(model p) - Brier(closing no-vig p) on the same picks; the
   whole paired 95% range must be below 0.
3. **Profit after the margin:** EV at close = closing no-vig p x decimal odds - 1, averaged;
   the whole 95% range must be above 0.

The closing no-vig probability of the side at the picked line is `book_prob + clv_consensus`
(the ledger's CLV is close - book no-vig at pick). The CLV is also split into market movement
(close - consensus at pick) and book difference (consensus at pick - book no-vig at pick).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

N_WORKS = 200
ECE_MAX = 0.05
BINS = (0.0, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70, 1.0001)
RETIRED = {"G1": "2026-11-03", "G2": "2026-11-03"}  # MODEL.md "M20: Part A"
# archived from the NBA card when the engine became its source (owner override 2026-11-04,
# MODEL.md "Engine card"); since 2026-11-05 they generate candidates only for the shadow Card v1
# (never pushed, never official). Every pick stays in the ledger and its record here.
ARCHIVED = {"C-NBA": "2026-11-04 (nba15/nbap15 repricing)", "F": "2026-11-04 (nbap15)"}


def _f(x) -> float | None:
    if x in ("", None):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def decimal_odds(american: float) -> float:
    return 1 + american / 100 if american > 0 else 1 + 100 / abs(american)


def mean_ci(xs: list[float]) -> tuple[float | None, float | None, float | None]:
    """(mean, low, high): mean +/- 1.96 sd / sqrt(n)."""
    n = len(xs)
    if n == 0:
        return None, None, None
    m = sum(xs) / n
    if n < 2:
        return m, None, None
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    h = 1.96 * sd / math.sqrt(n)
    return m, m - h, m + h


@dataclass
class Pick:
    p: float | None  # the model's (fair) probability of the picked side
    close: float | None  # the closing no-vig probability of the side at the line
    y: int  # 1 win, 0 loss
    dec: float  # decimal odds taken
    cons_at_pick: float | None = None
    book: float | None = None


@dataclass
class Check:
    name: str
    value: float | None
    low: float | None
    high: float | None
    passed: bool | None  # None: not computable
    n: int
    note: str = ""


@dataclass
class Checks:
    n: int
    calibration: Check
    brier: Check
    ev: Check
    table: list[dict] = field(default_factory=list)
    clv: tuple = (None, None, None)
    move: tuple = (None, None, None)
    book_diff: tuple = (None, None, None)
    n_split: int = 0

    @property
    def works(self) -> bool:
        return (self.n >= N_WORKS
                and all(c.passed for c in (self.calibration, self.brier, self.ev)))  # fmt: skip

    @property
    def status(self) -> str:
        if self.works:
            return "works"
        failed = [c.name for c in (self.calibration, self.brier, self.ev) if c.passed is False]
        if self.n < N_WORKS:
            return f"too few ({self.n}/{N_WORKS})" + (f"; failing so far: {', '.join(failed)}"
                                                       if failed else "")  # fmt: skip
        return "does not work: fails " + ", ".join(failed or ["(not computable)"])


def picks_of(rows: list[dict]) -> list[Pick]:
    """Graded win/loss picks of ledger rows (official.dataset or ledger.joined rows)."""
    out = []
    for r in rows:
        if r.get("result") not in ("win", "loss"):
            continue
        book, clv = _f(r.get("book_prob")), _f(r.get("clv_consensus"))
        close = None if book is None or clv is None else book + clv
        cons = None
        if r.get("consensus_at_pick"):
            try:
                cons = _f(json.loads(r["consensus_at_pick"]).get("prob"))
            except (ValueError, AttributeError):
                cons = None
        out.append(Pick(_f(r.get("fair_prob")), close, int(r["result"] == "win"),
                        decimal_odds(float(r["odds"])), cons, book))  # fmt: skip
    return out


def reliability(ps: list[float], ys: list[int]) -> tuple[list[dict], float | None]:
    table, ece, n = [], 0.0, len(ps)
    for lo, hi in zip(BINS, BINS[1:], strict=False):
        idx = [i for i, p in enumerate(ps) if lo <= p < hi]
        if not idx:
            continue
        mp = sum(ps[i] for i in idx) / len(idx)
        wr = sum(ys[i] for i in idx) / len(idx)
        table.append({"bin": f"{lo:.2f}-{min(hi, 1):.2f}", "n": len(idx),
                      "predicted": round(mp, 4), "actual": round(wr, 4)})
        ece += len(idx) / n * abs(mp - wr)
    return table, (ece if n else None)


def three_checks(picks: list[Pick]) -> Checks:
    n = len(picks)
    wp = [x for x in picks if x.p is not None]
    table, ece = reliability([x.p for x in wp], [x.y for x in wp])
    gap = mean_ci([x.p - x.y for x in wp])
    cal = Check("calibration", ece, gap[1], gap[2],
                None if len(wp) < 2 or ece is None or gap[1] is None
                else (gap[1] <= 0 and ece <= ECE_MAX), len(wp),
                "" if wp else "no model probability")  # fmt: skip
    if gap[0] is not None:
        cal.note = f"gap {100 * gap[0]:+.1f} pp"
    both = [x for x in wp if x.close is not None]
    d = mean_ci([(x.p - x.y) ** 2 - (x.close - x.y) ** 2 for x in both])
    bri = Check("brier vs close", d[0], d[1], d[2],
                None if d[2] is None else d[2] < 0, len(both))
    wc = [x for x in picks if x.close is not None]
    e = mean_ci([x.close * x.dec - 1 for x in wc])
    ev = Check("EV at close", e[0], e[1], e[2], None if e[1] is None else e[1] > 0, len(wc))
    split = [x for x in wc if x.cons_at_pick is not None and x.book is not None]
    return Checks(n, cal, bri, ev, table,
                  mean_ci([x.close - x.book for x in wc if x.book is not None]),
                  mean_ci([x.close - x.cons_at_pick for x in split]),
                  mean_ci([x.cons_at_pick - x.book for x in split]), len(split))  # fmt: skip


def by_signal(rows: list[dict]) -> dict[str, Checks]:
    """Three checks per signal name over counted ledger rows."""
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["signal_name"], []).append(r)
    return {k: three_checks(picks_of(v)) for k, v in sorted(by.items())}


def _pp(x: float | None, scale: float = 100, nd: int = 1) -> str:
    return "–" if x is None else f"{scale * x:+.{nd}f}"


def _rng(c: Check, scale: float, nd: int) -> str:
    return "" if c.low is None else f" ({_pp(c.low, scale, nd)} to {_pp(c.high, scale, nd)})"


def summary_line(c: Checks) -> str:
    """One line for the Research tab and the CLI."""
    cal, b, e = c.calibration, c.brier, c.ev
    cal_txt = ("n/a" if cal.value is None
               else f"ECE {cal.value:.3f}, {cal.note}{_rng(cal, 100, 1)} {_ok(cal.passed)}")
    return (f"Calibration {cal_txt} · Brier vs close {_pp(b.value, 1, 4)}{_rng(b, 1, 4)} "
            f"{_ok(b.passed)} · EV at close {_pp(e.value)}%{_rng(e, 100, 1)} {_ok(e.passed)}"
            f" · {c.status}")


def _ok(x: bool | None) -> str:
    return {True: "pass", False: "fail", None: "n/a"}[x]


def report(rows: list[dict]) -> str:
    """The CLI's tables: per signal, the three checks, the reliability table and the CLV
    split."""
    out = []
    for name, c in by_signal(rows).items():
        tag = f" (retired {RETIRED[name]})" if name in RETIRED else ""
        out.append(f"== {name}{tag}: {c.n} graded (win/loss)")
        out.append("  " + summary_line(c))
        m, lo, hi = c.clv
        out.append(f"  CLV {_pp(m)} pp" + ("" if lo is None else f" ({_pp(lo)} to {_pp(hi)})")
                   + f" = market movement {_pp(c.move[0])} + book difference "
                   f"{_pp(c.book_diff[0])} (split on {c.n_split} picks)")  # fmt: skip
        for b in c.table:
            out.append(f"    p {b['bin']}: n {b['n']}, predicted {b['predicted']:.3f}, "
                       f"won {b['actual']:.3f}")
    return "\n".join(out)
