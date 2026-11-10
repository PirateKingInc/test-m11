"""M19: the official record and the research records (MODEL.md "M19"). Every number the page
shows comes from here, over the ledger CSVs only.

- `dataset`: every counted pick (no M16 legacy alert, no duplicate) with its result, card fields
  and review fields; `day` is the card date (or the tip's Eastern date).
- `record`: graded picks only (win / loss / push), like a team's record: W-L-P, units, ROI,
  average odds, CLV and the number of graded picks. Pending picks appear only under Today and
  voids only in the day view (owner correction 2026-11-03): neither is ever in or beside a record.
- Official picks are card picks, at the card stake; research records are flat 1 unit.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

GRADED = ("win", "loss", "push")
REVIEW_PICK = "review_pick.csv"
REVIEW_RESULT = "review_result.csv"
CSV_FIELDS = ("pick_id", "time", "sport", "game", "team", "opponent", "player", "market",
              "pick", "line", "odds", "book", "card", "label", "stake", "result",
              "actual_stat", "units", "clv", "review_tag")  # fmt: skip


def _f(x) -> float | None:
    return None if x in ("", None) else float(x)


def _et_date(iso: str) -> str:
    from betdata.alerts.rules import EASTERN

    return datetime.fromisoformat(iso).astimezone(EASTERN).date().isoformat() if iso else ""


def dataset(out: Path) -> list[dict]:
    """Every counted ledger pick, with review fields joined (blank where none)."""
    from betdata.ledger import counted, joined, read_rows

    rp = {r["pick_id"]: r for r in read_rows(out / REVIEW_PICK)}
    rr = {r["pick_id"]: r for r in read_rows(out / REVIEW_RESULT)}
    sh = {r["pick_id"]: r for r in read_rows(out / "shadow_card.csv")}
    sp = {r["pick_id"]: r for r in read_rows(out / "spread_card.csv")}  # Spread Picks
    rows = []
    for r in counted(joined(out), spread=True):
        p, q = rp.get(r["pick_id"], {}), rr.get(r["pick_id"], {})
        official = r.get("card") == "yes"
        x = sh.get(r["pick_id"], {})
        y = sp.get(r["pick_id"], {})
        rows.append({**r, "official": official,
                     "spread": y.get("spread") == "yes", "spread_date": y.get("card_date", ""),
                     "spread_label": y.get("spread_label", ""),
                     "spread_stake": y.get("spread_stake", ""),
                     "spread_trigger": y.get("trigger_pick_id", ""),
                     "spread_backfill": y.get("backfill") == "true",
                     "shadow": x.get("shadow") == "yes", "shadow_date": x.get("card_date", ""),
                     "shadow_label": x.get("shadow_label", ""),
                     "shadow_stake": x.get("shadow_stake", ""),
                     "day": r.get("card_date") or _et_date(r["tip"]),
                     "team": p.get("team", ""), "opponent": p.get("opponent", ""),
                     "proj_median": p.get("proj_median", ""),
                     "proj_minutes": p.get("proj_minutes", ""),
                     "actual_stat": q.get("actual_stat", ""),
                     "actual_minutes": q.get("actual_minutes", ""),
                     "review_tag": q.get("tag", "")})  # fmt: skip
    return rows


def official_stake(r: dict) -> float:
    return _f(r.get("card_stake")) or 0.0


def shadow_stake(r: dict) -> float:
    return _f(r.get("shadow_stake")) or 0.0


def flat(_r: dict) -> float:
    return 1.0


def american(dec: float) -> int:
    return round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))


@dataclass
class Rec:
    """A record of graded picks only (no pending, no void)."""

    wins: int = 0
    losses: int = 0
    pushes: int = 0
    units: float = 0.0
    staked: float = 0.0
    decimals: list = field(default_factory=list)
    clvs: list = field(default_factory=list)
    rows: list = field(default_factory=list)  # the graded picks behind it (drill-down CSV)

    @property
    def graded(self) -> int:
        return self.wins + self.losses + self.pushes

    @property
    def roi(self) -> float | None:
        return self.units / self.staked if self.staked else None

    @property
    def avg_odds(self) -> int | None:
        return american(sum(self.decimals) / len(self.decimals)) if self.decimals else None

    @property
    def clv(self) -> float | None:
        return 100 * sum(self.clvs) / len(self.clvs) if self.clvs else None

    @property
    def wlp(self) -> str:
        return f"{self.wins}-{self.losses}-{self.pushes}"


def graded(rows: list[dict]) -> list[dict]:
    return [r for r in rows if (r.get("result") or "") in GRADED]


def voids(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("result") == "void"]


def pending(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not r.get("result")]


def record(rows: list[dict], stake=flat) -> Rec:
    """Graded picks only: pending and void picks are skipped, never counted (MODEL.md "M19")."""
    from betdata.alerts.rules import decimal

    rec = Rec()
    for r in rows:
        res = r.get("result") or ""
        if res not in GRADED:
            continue
        rec.rows.append(r)
        rec.wins += res == "win"
        rec.losses += res == "loss"
        rec.pushes += res == "push"
        s = stake(r)
        rec.units += s * (_f(r.get("profit")) or 0.0)
        rec.staked += s
        rec.decimals.append(decimal(int(r["odds"])))
        if (c := _f(r.get("clv_consensus"))) is not None:
            rec.clvs.append(c)
    return rec


def official(rows: list[dict], sport: str | None = None) -> list[dict]:
    return [r for r in rows if r["official"] and (sport is None or r["sport"] == sport)]


def goal_rows(rows: list[dict], sport: str, card: str | None = None) -> list[dict]:
    """The official rows a success goal counts: an NBA card's own (betdata/cards.py: "Next
    Gen" or "OG", owner decision 2026-11-05), or the sport's card (NFL)."""
    if sport == "nba":
        from betdata import cards as C

        return C.view(rows, card or C.NEXTGEN)
    return official(rows, sport)


def periods(rows: list[dict], today: date) -> dict[str, Rec]:
    """All time, last 7 days (card dates today-6 .. today) and yesterday, at card stakes."""
    t = today.isoformat()
    week = (today - timedelta(days=6)).isoformat()
    yest = (today - timedelta(days=1)).isoformat()
    return {"All time": record(rows, official_stake),
            "Last 7 days": record([r for r in rows if week <= r["day"] <= t], official_stake),
            "Yesterday": record([r for r in rows if r["day"] == yest], official_stake)}


def by_day(rows: list[dict]) -> dict[str, list[dict]]:
    """Graded and void picks by day, newest first; pending picks are left out (they appear only
    under Today)."""
    out: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda r: (r["day"], r["tip"], r["game"])):
        if r.get("result"):
            out.setdefault(r["day"], []).append(r)
    return dict(sorted(out.items(), reverse=True))


def signals(rows: list[dict]) -> dict[str, Rec]:
    """Each signal's research record: all its picks, flat 1 unit."""
    from betdata.spread import NAME as SPREAD

    out: dict[str, list] = {}
    for r in rows:
        if r["signal_name"] != SPREAD:  # Spread Picks: their own record only (owner 2026-11-10)
            out.setdefault(r["signal_name"], []).append(r)
    return {k: record(v) for k, v in sorted(out.items())}


def tag_counts(rows: list[dict]) -> dict[str, dict[str, int]]:
    """{"win": {tag: n}, "loss": {tag: n}} over graded picks with a reason tag."""
    out: dict[str, dict[str, int]] = {"win": {}, "loss": {}}
    for r in rows:
        if r.get("result") in ("win", "loss") and r.get("review_tag"):
            d = out[r["result"]]
            d[r["review_tag"]] = d.get(r["review_tag"], 0) + 1
    return out


# --- the CSVs -----------------------------------------------------------------------------------


def pick_text(r: dict) -> str:
    from types import SimpleNamespace

    from betdata.alerts.notify import selection

    a = SimpleNamespace(game_label=r["game"] or "? @ ?", side=r["side"],
                        line=_f(r["line"]), player_name=r["player"] or None, market=r["market"])
    try:
        return selection(a)
    except (ValueError, TypeError):
        return f"{r['market']} {r['side']}"


def csv_row(r: dict) -> dict:
    """One pick as the daily CSV gives it (MODEL.md "M19": the column list)."""
    stake = official_stake(r) if r["official"] else (_f(r.get("stake")) or 1.0)
    res = r.get("result") or ""
    units = "" if res not in GRADED else round(stake * (_f(r.get("profit")) or 0.0), 4)
    clv = _f(r.get("clv_consensus"))
    return {"pick_id": r["pick_id"], "time": r["created_at"], "sport": r["sport"],
            "game": r["game"], "team": r.get("team", ""), "opponent": r.get("opponent", ""),
            "player": r["player"], "market": r["market"], "pick": pick_text(r),
            "line": r["line"], "odds": r["odds"], "book": r["book"],
            "card": "yes" if r["official"] else "no",
            "label": r.get("card_label") if r["official"] else r.get("label", ""),
            "stake": stake, "result": res or "pending", "actual_stat": r.get("actual_stat", ""),
            "units": units, "clv": "" if clv is None else round(100 * clv, 2),
            "review_tag": r.get("review_tag", "")}  # fmt: skip


def csv_text(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(CSV_FIELDS), lineterminator="\n")
    w.writeheader()
    w.writerows(csv_row(r) for r in rows)
    return buf.getvalue()


def write_csv(path: Path, rows: list[dict]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(csv_text(rows))
    return path.name


# --- The official success goal (owner decision 2026-11-03; MODEL.md "Official success goal") ---

GOAL_N = 500  # graded card picks


@dataclass
class Goal:
    """The card at .500 or better AND positive in units, over at least GOAL_N graded picks."""

    wins: int
    losses: int
    pushes: int
    units: float
    graded: int
    avg_odds: int | None
    break_even: float | None  # win rate that breaks even at the card's average odds

    @property
    def pct(self) -> float | None:
        d = self.wins + self.losses  # pushes are neither
        return self.wins / d if d else None

    @property
    def win_ok(self) -> bool:
        return self.pct is not None and self.pct >= 0.5

    @property
    def units_ok(self) -> bool:
        return self.units > 0

    @property
    def status(self) -> str:
        """Under GOAL_N: "too early", with "on track" (both conditions met now) or "off track"
        (either not met) once there is a graded pick. At GOAL_N or more: "goal met" or
        "goal not met"."""
        both = self.win_ok and self.units_ok
        if self.graded >= GOAL_N:
            return "goal met" if both else "goal not met"
        if self.graded == 0:
            return "too early"
        return "too early · " + ("on track" if both else "off track")


def goal(rows: list[dict]) -> Goal:
    """The goal over official (card) rows, graded only, units at the card stake."""
    r = record(rows, official_stake)
    dec = sum(r.decimals) / len(r.decimals) if r.decimals else None
    return Goal(r.wins, r.losses, r.pushes, r.units, r.graded, r.avg_odds,
                None if dec is None else 1 / dec)  # fmt: skip


def goal_text(g: Goal) -> str:
    """'Goal: .500+ and positive units over 500 graded picks — 6-5 (.545), +1.07u · 11 of 500
    · too early · on track'."""
    pct = "–" if g.pct is None else f"{g.pct:.3f}".lstrip("0")
    return (f"Goal: .500+ and positive units over {GOAL_N} graded picks — {g.wins}-{g.losses}"
            f"{f'-{g.pushes}' if g.pushes else ''} ({pct}), {g.units:+.2f}u · "
            f"{g.graded} of {GOAL_N} · {g.status}")


def context_text(g: Goal) -> str:
    """'avg odds +119 · break-even 45.7%' (the .500 target read in context)."""
    o = "–" if g.avg_odds is None else (f"+{g.avg_odds}" if g.avg_odds > 0 else str(g.avg_odds))
    be = "–" if g.break_even is None else f"{100 * g.break_even:.1f}%"
    return f"avg odds {o} · break-even {be}"


# --- Engine card vs shadow Card v1 (owner request 2026-11-05; MODEL.md "Shadow Card v1") -------

COMPARE_N = 200  # graded picks each, before any comparison


def same_nights(out: Path) -> set[str]:
    """NBA card dates on which both Next Gen (`engine`) and OG (`shadow`) were selected."""
    from betdata.ledger import read_rows

    kinds: dict[str, set] = {}
    for r in read_rows(out / "card_days.csv"):
        if r["sport"] == "nba":
            kinds.setdefault(r["card_date"], set()).add(r["kind"])
    return {d for d, k in kinds.items() if {"engine", "shadow"} <= k}


@dataclass
class Side:
    name: str
    rec: Rec
    checks: object  # betdata.checks.Checks


@dataclass
class Comparison:
    nights: int
    engine: Side  # Next Gen
    shadow: Side  # OG

    @property
    def verdict(self) -> str:
        """The fixed rule (MODEL.md "Shadow Card v1: comparison rule", relabelled Next Gen vs
        OG on 2026-11-05)."""
        e, s = self.engine, self.shadow
        if e.rec.graded < COMPARE_N or s.rec.graded < COMPARE_N:
            return (f"too early to compare: Next Gen {e.rec.graded}/{COMPARE_N}, OG "
                    f"{s.rec.graded}/{COMPARE_N} graded")
        ee, se = e.checks.ev, s.checks.ev
        apart = se.low is not None and ee.high is not None and se.low > ee.high
        if apart and s.rec.units > 0:
            return "OG is ahead by the rule (retiring a card is the owner's decision)"
        return "no card is ahead by the rule"


def comparison(rows: list[dict], out: Path) -> Comparison:
    """Next Gen vs OG over the same nights only (both selected), each at its own stakes."""
    from betdata import cards as C
    from betdata.checks import picks_of, three_checks

    nights = same_nights(out)
    eng = [r for r in C.view(rows, C.NEXTGEN) if r["day"] in nights]
    og = [r for r in C.view(rows, C.OG) if r["day"] in nights]
    er, sr = record(eng, official_stake), record(og, official_stake)
    return Comparison(len(nights), Side(C.name(C.NEXTGEN), er, three_checks(picks_of(er.rows))),
                      Side(C.name(C.OG), sr, three_checks(picks_of(sr.rows))))


def comparison_rows(c: Comparison) -> list[tuple[str, str, str]]:
    """(metric, Next Gen, OG) for the table (Research tab, weekly report)."""

    def odds(r: Rec) -> str:
        o = r.avg_odds
        return "–" if o is None else (f"+{o}" if o > 0 else str(o))

    def ev(ck) -> str:
        e = ck.ev
        if e.value is None:
            return "–"
        rng = "" if e.low is None else f" (95% {100 * e.low:+.1f} to {100 * e.high:+.1f})"
        return f"{100 * e.value:+.1f}%{rng}"

    def cal(ck) -> str:
        c = ck.calibration
        return "–" if c.value is None else f"ECE {c.value:.3f}, {c.note}"

    def roi(r: Rec) -> str:
        return "–" if r.roi is None else f"{100 * r.roi:+.1f}%"

    e, s = c.engine, c.shadow
    return [("Graded picks", str(e.rec.graded), str(s.rec.graded)),
            ("W-L-P", e.rec.wlp, s.rec.wlp),
            ("Units", f"{e.rec.units:+.2f}u", f"{s.rec.units:+.2f}u"),
            ("ROI", roi(e.rec), roi(s.rec)),
            ("Average odds", odds(e.rec), odds(s.rec)),
            ("EV at close", ev(e.checks), ev(s.checks)),
            ("Calibration", cal(e.checks), cal(s.checks))]  # fmt: skip


COMPARE_RULE = (f"Comparison rule (fixed 2026-11-05): compare only once both cards have at least "
                f"{COMPARE_N} graded picks over the same nights (only nights on which both were "
                "selected); OG is ahead only if its EV at close is higher with the 95% ranges not "
                "overlapping AND its units are positive. Retiring either card stays the owner's "
                "decision.")
