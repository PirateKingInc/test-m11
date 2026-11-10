"""The Spread Picks review (owner decision 2026-11-10, MODEL.md "Spread Picks card"): at 50
graded Spread picks a report and one ntfy line; nothing turns off (the owner decides). Also the
quiet comparison (report only): every Next Gen ML underdog prediction NOT on the card, and what
its spread did, from `ledger/engine_grades.csv` (never picks.csv).

`betdata ledger spread-review` writes the report at any time; the milestone writes it once.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path

from betdata.spread import REVIEW_N

MILESTONE = "spread-card"  # milestones.csv signal_name of the 50-pick review
REPORT = Path("reports") / "nba" / "spread_review.md"


def _f(x) -> float | None:
    return None if x in ("", None) else float(x)


def spread_rows(out: Path) -> list[dict]:
    from betdata import cards as C
    from betdata import official as O

    return C.view(O.dataset(out), C.SPREAD)


def graded_n(out: Path) -> int:
    from betdata import official as O

    return O.record(spread_rows(out), O.official_stake).graded


def _scores(m, gids: set) -> dict:
    """{game_id: (home, away)} final scores from the main database (None: no database)."""
    if m is None or not gids:
        return {}
    from sqlalchemy import select

    from betdata.db.schema import results

    out = {}
    for r in m.execute(select(results.c.game_id, results.c.home_score, results.c.away_score)
                       .where(results.c.game_id.in_([int(g) for g in gids]),
                              results.c.final.is_(True))):  # fmt: skip
        if r.home_score is not None and r.away_score is not None:
            out.setdefault(r.game_id, (r.home_score, r.away_score))
    return out


def _margin(r: dict, sc: tuple | None) -> float | None:
    """How far the pick covered (+) or missed (-): the side's margin plus its line."""
    if sc is None or _f(r.get("line")) is None:
        return None
    home, away = sc
    return (home - away) * (1 if r["side"] == "home" else -1) + float(r["line"])


def quiet_comparison(out: Path) -> list[dict]:
    """Every Next Gen ML underdog prediction not on the Next Gen card (engine_grades.csv: the
    engine sides with the market's underdog, its probability above the close), with its ML and
    spread results at the stored lines. Report only."""
    from betdata.ledger import read_rows

    on_card = set()
    picks = {r["pick_id"]: r for r in read_rows(out / "picks.csv")}
    for c in read_rows(out / "card.csv"):
        p = picks.get(c["pick_id"])
        if c["card"] == "yes" and p and p["market"] == "h2h" and p["signal"] == "engine":
            on_card.add((p["game_id"], p["side"]))
    by: dict[str, dict] = {}
    for r in read_rows(out / "engine_grades.csv"):
        if r.get("kind") == "game" and r["market"] in ("h2h", "spreads"):
            by.setdefault(r["game_id"], {})[r["market"]] = r
    flip = {"win": "loss", "loss": "win"}
    rows = []
    for gid, d in sorted(by.items(), key=lambda kv: kv[1].get("h2h", {}).get("tip", "")):
        h = d.get("h2h")
        if h is None or _f(h["p_close"]) is None or _f(h["p_engine"]) is None:
            continue
        pe, pc = float(h["p_engine"]), float(h["p_close"])
        dog = "home" if pc < 0.5 else "away"
        if (dog == "home" and pe <= pc) or (dog == "away" and pe >= pc):
            continue  # the engine does not side with the underdog
        if (gid, dog) in on_card:
            continue
        ml = h["result"] if dog == h["side"] else flip.get(h["result"], h["result"])
        s = d.get("spreads")
        ats = line = None
        if s is not None and s.get("result"):
            ats = s["result"] if dog == s["side"] else flip.get(s["result"], s["result"])
            ln = _f(s["line"])
            line = None if ln is None else (ln if dog == s["side"] else -ln)
        team = h["game"].split(" @ ")[1 if dog == "home" else 0]
        rows.append({"game_id": gid, "game": h["game"], "tip": h["tip"], "team": team,
                     "ml": ml, "ats": ats, "line": line})  # fmt: skip
    return rows


def _wl(res: list) -> str:
    c = Counter(res)
    return f"{c['win']}-{c['loss']}" + (f"-{c['push']}" if c["push"] else "")


def report(out: Path, now: datetime, m=None) -> str:
    """The review report (markdown)."""
    from betdata import official as O
    from betdata.checks import picks_of, three_checks
    from betdata.ledger import read_rows

    rows = spread_rows(out)
    rec = O.record(rows, O.official_stake)
    g = O.graded(rows)
    res = {r["pick_id"]: r for r in read_rows(out / "results.csv")}
    sc = _scores(m, {r["game_id"] for r in g})
    n_bf = sum(bool(r.get("spread_backfill")) for r in rows)
    lines = [f"# Spread Picks review ({now.date().isoformat()})", "",
             f"Owner decision 2026-11-10: reviewed at {REVIEW_N} graded Spread picks; nothing "
             "turns off by itself, the owner decides. 1 unit flat at the real prices.", "",
             "## Record", "",
             f"- Graded: {rec.graded} ({rec.wlp}), units {rec.units:+.2f}u, ROI "
             + ("–" if rec.roi is None else f"{100 * rec.roi:+.1f}%"),
             "- Average line: " + ("–" if not g else
                                    f"{sum(float(r['line']) for r in g) / len(g):+.1f}"),
             "- Average price: " + ("–" if rec.avg_odds is None else f"{rec.avg_odds:+d}"),
             f"- Reconstructed (backfilled) prices: {n_bf}",
             ""]  # fmt: skip
    ev = three_checks(picks_of(g)).ev
    lines += ["## EV at close", "",
              ("No closes yet for these picks." if ev.value is None else
               f"EV at close {100 * ev.value:+.1f}% over {ev.n} picks with a close"
               + ("" if ev.low is None else
                  f" (95% {100 * ev.low:+.1f} to {100 * ev.high:+.1f})")), ""]  # fmt: skip
    lines += ["## ATS vs the same teams' ML results", "",
              "| Day | Game | Pick | Price | ATS | ML (Next Gen) | Cover margin |",
              "|---|---|---|---|---|---|---|"]
    ats, ml, margins = [], [], []
    for r in sorted(g, key=lambda r: (r["day"], r["game"])):
        t = res.get(r.get("spread_trigger", ""), {}).get("result", "")
        mg = _margin(r, sc.get(int(r["game_id"])))
        ats.append(r["result"])
        if t:
            ml.append(t)
        if mg is not None:
            margins.append(mg)
        team = r["game"].split(" @ ")[1 if r["side"] == "home" else 0]
        lines.append(f"| {r['day']} | {r['game']} | {team} {float(r['line']):+g} | "
                     f"{int(r['odds']):+d} | {r['result']} | {t or '–'} | "
                     f"{'–' if mg is None else f'{mg:+g}'} |")
    lines += ["", f"ATS {_wl(ats)} vs ML {_wl(ml)} on the same picks.", ""]
    lines += ["## Cover margin distribution", ""]
    if not margins:
        lines.append("No final scores available here (run on a runner with the main database).")
    else:
        bins = [(-99, -10), (-10, -5), (-5, 0), (0, 0), (0, 5), (5, 10), (10, 99)]
        for lo, hi in bins:
            n = (sum(x == 0 for x in margins) if lo == hi == 0 else
                 sum(lo <= x < hi for x in margins) if hi <= 0 else
                 sum(lo < x <= hi for x in margins))  # fmt: skip
            lab = "push (0)" if lo == hi == 0 else f"{lo:+d} to {hi:+d}"
            lines.append(f"- {lab}: {n}")
    q = quiet_comparison(out)
    qg = [x for x in q if x["ats"]]
    lines += ["", "## Quiet comparison (report only): Next Gen ML underdogs not on the card", "",
              "From `ledger/engine_grades.csv`: every engine prediction siding with the market's "
              "ML underdog (engine probability above the close) that was not a Next Gen card "
              "pick, at the stored closing-snapshot lines. Never a pick.", "",
              f"- {len(q)} predictions: ML {_wl([x['ml'] for x in q if x['ml']])}, ATS "
              f"{_wl([x['ats'] for x in qg])} ({len(qg)} with a graded spread)", ""]  # fmt: skip
    return "\n".join(lines) + "\n"


def write(root: Path, now: datetime, m=None) -> Path:
    path = root / REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report(root / "ledger", now, m))
    return path


def milestone(out: Path, now: datetime) -> list[dict]:
    """The 50-pick review milestone, once (milestones.csv, signal_name `spread-card`)."""
    from betdata.ledger import _append_milestones, _iso, read_rows

    done = {(r["signal_name"], r["graded"]) for r in read_rows(out / "milestones.csv")}
    if (MILESTONE, str(REVIEW_N)) in done or graded_n(out) < REVIEW_N:
        return []
    new = [{"signal_name": MILESTONE, "graded": str(REVIEW_N), "reached_at": _iso(now)}]
    _append_milestones(out / "milestones.csv", new)
    return new


def ntfy_line(out: Path) -> str:
    from betdata import official as O

    rec = O.record(spread_rows(out), O.official_stake)
    roi = "–" if rec.roi is None else f"{100 * rec.roi:+.1f}%"
    return (f"Spread Picks reached {REVIEW_N} graded picks: {rec.wlp}, {rec.units:+.2f}u, ROI "
            f"{roi} — review report in {REPORT.as_posix()} (nothing turns off; owner decides)")
