"""M19: the official record and the research records (MODEL.md "M19"). Every number the page
shows comes from here, over the ledger CSVs only.

- `dataset`: every counted pick (no M16 legacy alert, no duplicate) with its result, card fields
  and review fields; `day` is the card date (or the tip's Eastern date).
- `record`: graded picks only (win / loss / push). Void and pending picks are counted in the
  reconciliation line, never in W-L-P, units, ROI, average odds or CLV.
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
    rows = []
    for r in counted(joined(out)):
        p, q = rp.get(r["pick_id"], {}), rr.get(r["pick_id"], {})
        official = r.get("card") == "yes"
        rows.append({**r, "official": official,
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


def flat(_r: dict) -> float:
    return 1.0


def american(dec: float) -> int:
    return round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))


@dataclass
class Rec:
    picks: int = 0
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    voids: int = 0
    pending: int = 0
    units: float = 0.0
    staked: float = 0.0
    decimals: list = field(default_factory=list)
    clvs: list = field(default_factory=list)
    rows: list = field(default_factory=list)  # the picks behind it, for the drill-down CSV

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

    def line(self) -> str:
        """The reconciliation line: every pick is graded, void or pending."""
        return (f"{self.picks} pick{'s' if self.picks != 1 else ''} = {self.graded} graded "
                f"({self.wlp}) + {self.voids} void + {self.pending} pending")


def record(rows: list[dict], stake=flat) -> Rec:
    """Graded picks only enter W-L-P, units, ROI, average odds and CLV (MODEL.md "M19")."""
    from betdata.alerts.rules import decimal

    rec = Rec()
    for r in rows:
        rec.picks += 1
        rec.rows.append(r)
        res = r.get("result") or ""
        if res == "void":
            rec.voids += 1
            continue
        if res not in GRADED:
            rec.pending += 1
            continue
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


def periods(rows: list[dict], today: date) -> dict[str, Rec]:
    """All time, last 7 days (card dates today-6 .. today) and yesterday, at card stakes."""
    t = today.isoformat()
    week = (today - timedelta(days=6)).isoformat()
    yest = (today - timedelta(days=1)).isoformat()
    return {"All time": record(rows, official_stake),
            "Last 7 days": record([r for r in rows if week <= r["day"] <= t], official_stake),
            "Yesterday": record([r for r in rows if r["day"] == yest], official_stake)}


def by_day(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda r: (r["day"], r["tip"], r["game"])):
        out.setdefault(r["day"], []).append(r)
    return dict(sorted(out.items(), reverse=True))


def signals(rows: list[dict]) -> dict[str, Rec]:
    """Each signal's research record: all its picks, flat 1 unit."""
    out: dict[str, list] = {}
    for r in rows:
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
