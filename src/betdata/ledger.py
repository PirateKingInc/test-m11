"""The permanent pick ledger (owner request 2026-10-30; reporting only, no rule changes).

Every alert is a pick: the M18 signals (C-NBA, C-NFL, F, G1, G2), the M16 paper alerts, and any
future signal. The ledger has two parts.
- **Pick fields** (`ledger/picks.csv`), written once when the pick first appears: id, time,
  sport, game, market, player, side, line, odds, book, signal, edge-source tag, reason,
  repriced fair price, the consensus at pick time.
- **Result fields** (`ledger/results.csv`), written once when the result is final: result,
  profit (1 unit at the alerted price), CLV vs the consensus close and vs Pinnacle's close,
  grade time. Voids are kept but excluded from W-L and ROI.

**Durability:** the poller database lives in the Actions cache and can be lost, so the CSVs in
the repo are the record. Each hourly run appends what is new and commits to main. Rows are only
ever appended: `append_rows` refuses any write whose old text is not a prefix of the new, and
tests/test_ledger.py checks the files' git history. If the poller database is rebuilt,
`reload_alerts` puts every ledger pick back as an alert, so grading continues.

**Pick id:** a hash of the alert's de-duplication key, stable across database rebuilds.

**Annotations** (`ledger/annotations.csv`, owner decision 2026-10-30): pick rows are never
rewritten, so a later correction to a pick is an append-only annotation (pick id, field, value)
that `joined` lays over the pick. Two are written by `sync`:
- the M16 paper alerts (signal `news`, pre-M18 rules) are renamed `m16-legacy`;
- the 2 M16 alerts that repeated an injury change already alerted (REVIEW.md M16-4) are marked
  `duplicate: true`.
The record (page, weekly report, milestones) counts neither: legacy and duplicate picks stay in
the CSV only.

**Labels and stakes** (`ledger/labels.csv`, owner request 2026-10-30; alerts/labels.py): each
pick created since labels shipped carries a label (TAKE 1 unit, LEAN 0.5, PASS 0) fixed by the
poller when the alert was created. `sync` writes it once, keyed by pick id, and never again;
older picks have no label (no backfill). The simulated bankroll (`bank`) starts at 100 units and
adds stake x profit for each settled labelled pick (flat stakes, no compounding).

**Tonight's card** (`ledger/card.csv`, owner request 2026-10-30; alerts/card.py): once a
sport's card is chosen (poller state `card:<sport>:<day>`), `sync` writes one row per pick on
that day's games, once: `card` yes (with the card's own label and stake, `card_label` and
`card_stake`, set at selection) or no (not chosen, or created after selection). The card
bankroll counts card picks only. The creation-time `label` and `stake` above stay as they are.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import Connection, insert, inspect, select

log = logging.getLogger(__name__)

PICK_FIELDS = ("pick_id", "dedup_key", "created_at", "sport", "game_id", "game", "tip",
               "market", "player", "side", "line", "odds", "book", "signal", "signal_name",
               "edge_source", "news_move", "reason", "fair_prob", "book_prob",
               "consensus_at_pick")  # fmt: skip
RESULT_FIELDS = ("pick_id", "result", "profit", "clv_consensus", "close_method",
                 "clv_pinnacle", "graded_at")  # fmt: skip
MILESTONE_FIELDS = ("signal_name", "graded", "reached_at")
ANNOTATION_FIELDS = ("key", "pick_id", "field", "value", "noted_at", "note")
LABEL_FIELDS = ("pick_id", "label", "stake", "labeled_at", "edge", "threshold", "edge_source",
                "pinnacle_sides", "available")  # fmt: skip
CARD_FIELDS = ("pick_id", "sport", "card_date", "card", "card_label", "card_stake",
               "selected_at", "ev", "edge", "threshold", "edge_source",
               "pinnacle_sides")  # fmt: skip
LEGACY = "m16-legacy"
# REVIEW.md M16-4: the next injury report repeated a change already alerted (Edgecombe DET @ PHI
# 7:00 PM; Zubac/Nembhard IND @ MIL 3:00 PM).
M16_DUPLICATES = ("P89c0ffdfda99", "Pddaf70680c30")
FINAL = ("win", "loss", "push", "void")
MILESTONES = (60, 200)
TOO_FEW = 100


def ledger_dir() -> Path:
    from betdata.config import PROJECT_ROOT

    return PROJECT_ROOT / "ledger"


def pick_id(dedup_key: str) -> str:
    return "P" + hashlib.sha1(dedup_key.encode()).hexdigest()[:12]


def _iso(t) -> str:
    if t is None:
        return ""
    t = t if t.tzinfo else t.replace(tzinfo=UTC)
    return t.astimezone(UTC).isoformat(timespec="seconds")


def _num(x, nd: int = 4) -> str:
    return "" if x is None else (str(round(float(x), nd)) if not isinstance(x, int) else str(x))


# --- append-only CSV ---------------------------------------------------------------------------


class AppendOnlyError(RuntimeError):
    pass


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def append_rows(path: Path, fields, rows: list[dict], key: str = "pick_id") -> int:
    """Append the rows whose `key` is not in the file yet; past rows are never touched. Returns
    rows appended. Raises AppendOnlyError if the file's header differs or the write would not
    keep the old text as its prefix."""
    old = path.read_text() if path.exists() else ""
    have = {r[key] for r in read_rows(path)} if old else set()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(fields), lineterminator="\n")
    if not old:
        w.writeheader()
    elif old.splitlines()[0] != ",".join(fields):
        raise AppendOnlyError(f"{path}: header differs from {fields}")
    new = [r for r in rows if r[key] not in have]
    seen = set()
    for r in new:
        if r[key] in seen:
            continue
        seen.add(r[key])
        w.writerow({f: r.get(f, "") for f in fields})
    text = old + ("" if not old or old.endswith("\n") else "\n") + buf.getvalue()
    if not text.startswith(old):
        raise AppendOnlyError(f"{path}: refusing a write that changes past rows")
    if text != old:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return len(seen)


# --- picks and results from the poller database --------------------------------------------------


def _details(a) -> dict:
    d = a.details
    if isinstance(d, str):
        try:
            return json.loads(d)
        except ValueError:
            return {}
    return d or {}


def backfill_news_move(a, p_over_fn=None) -> float | None:
    """What the repricer moved for a value alert stored before the tag existed: the fair price
    minus the side's probability without the news (MODEL.md "M18 Part C"; alerts/value.py)."""
    from betdata.alerts import consensus as C
    from betdata.alerts import value as V

    d = _details(a)
    if d.get("news_move") is not None:
        return float(d["news_move"])
    fair = d.get("fair", a.fair_prob)
    if fair is None:
        return None
    if a.player_name:
        mb, ma = d.get("mean_before"), d.get("mean_after")
        if p_over_fn is None or mb is None or ma is None or a.line is None:
            return None
        move = p_over_fn(a.market, ma, a.line) - p_over_fn(a.market, mb, a.line)
        return move if a.side == "over" else -move
    cb = d.get("consensus_before")
    if cb is not None:
        c = C.Consensus(a.sport, a.game_id, a.market, datetime.min, cb.get("method", "x"),
                        cb["fair"])  # fmt: skip
        base = c.prob(a.side, a.line)
    else:
        rp = SimpleNamespace(margin_before=d.get("margin_before"),
                             margin_after=d.get("margin_after"),
                             total_before=d.get("total_before"),
                             total_after=d.get("total_after"))  # fmt: skip
        if rp.margin_before is None:
            return None
        r = V.game_repricing(a.sport, a.market, None, rp)
        base = None if r is None else r.base(a.side, a.line)
    return None if base is None else float(fair) - base


def consensus_at(p: Connection, m: Connection | None, a) -> dict | None:
    """The consensus when the pick was made, with its probability of the side at the line."""
    from betdata.alerts import consensus as C
    from betdata.alerts.gates import close_prob

    t = a.created_at if a.created_at.tzinfo else a.created_at.replace(tzinfo=UTC)
    try:
        c = (C.prop_consensus(p, a.sport, a.game_id, a.market, a.player_name, t, a.line)
             or C.prop_consensus(p, a.sport, a.game_id, a.market, a.player_name, t)
             ) if a.player_name else C.game_consensus(p, m, a.sport, a.game_id, a.market, t)
    except Exception as e:  # noqa: BLE001 - a record field; never stops the ledger
        log.warning("consensus at pick %s: %s", a.dedup_key, e)
        return None
    if c is None:
        return None
    pr = close_prob(c, a)
    return {"method": c.method, "n": c.n, "two_book": c.two_book, "fair": round(c.fair, 4),
            "line": c.line, "prob": None if pr is None else round(pr, 4)}


def pick_row(p: Connection, m: Connection | None, a, p_over_fn=None) -> dict:
    from betdata.alerts.gates import signal_name
    from betdata.alerts.value import edge_source

    d = _details(a)
    news = tag = None
    if a.signal == "value":
        news = backfill_news_move(a, p_over_fn)
        edge = d.get("edge")
        if edge is None and a.fair_prob is not None and a.book_prob is not None:
            edge = a.fair_prob - a.book_prob
        tag = d.get("edge_source") or edge_source(news, edge)
    cons = consensus_at(p, m, a)
    return {"pick_id": pick_id(a.dedup_key), "dedup_key": a.dedup_key,
            "created_at": _iso(a.created_at), "sport": a.sport, "game_id": a.game_id,
            "game": a.game_label or "", "tip": _iso(a.tip), "market": a.market,
            "player": a.player_name or "", "side": a.side, "line": _num(a.line, 2),
            "odds": a.price, "book": a.book, "signal": a.signal,
            "signal_name": LEGACY if a.signal == "news" else signal_name(a.signal, a.sport),
            "edge_source": tag or "",
            "news_move": _num(news), "reason": " ".join((a.reason or "").split()),
            "fair_prob": _num(a.fair_prob), "book_prob": _num(a.book_prob),
            "consensus_at_pick": "" if cons is None else json.dumps(cons, sort_keys=True)}


def pinnacle_clv(m: Connection | None, p: Connection, a) -> float | None:
    """CLV vs Pinnacle's own last pre-game quote of the market, where one exists."""
    from betdata.alerts.grade import _main_obs, _poller_obs, clv_novig, latest_close, utc

    if a.tip is None:
        return None
    tip = utc(a.tip)
    obs = _poller_obs(p, a, "pinnacle", tip) + (_main_obs(m, a, "pinnacle", tip) if m else [])
    close = latest_close(obs, a.side, a.market, "pinnacle")
    return None if close is None else clv_novig(a, close)


def result_row(p: Connection, m: Connection | None, a, ag, cg, now: datetime) -> dict | None:
    if ag is None or ag.result not in FINAL:
        return None
    return {"pick_id": pick_id(a.dedup_key), "result": ag.result,
            "profit": _num(0.0 if ag.result == "void" else ag.profit),
            "clv_consensus": _num(None if cg is None else cg.clv),
            "close_method": "" if cg is None or cg.close_method is None else cg.close_method,
            "clv_pinnacle": _num(pinnacle_clv(m, p, a)), "graded_at": _iso(now)}


def _tables(conn) -> set[str]:
    return set(inspect(conn).get_table_names())


def sync(p: Connection, m: Connection | None, out: Path, now: datetime,
         p_over_fn=None) -> dict:  # fmt: skip
    """Append every new pick and every newly final result from the poller database to the
    ledger files; returns counts and the milestones reached."""
    from betdata.alerts.db import alert_grades, alerts, consensus_grades

    tabs = _tables(p)
    if "alerts" not in tabs:
        return {"picks": 0, "results": 0, "milestones": []}
    have_p = {r["pick_id"] for r in read_rows(out / "picks.csv")}
    have_r = {r["pick_id"] for r in read_rows(out / "results.csv")}
    ag = ({r.alert_id: r for r in p.execute(select(alert_grades))}
          if "alert_grades" in tabs else {})  # fmt: skip
    cg = ({r.alert_id: r for r in p.execute(select(consensus_grades))}
          if "consensus_grades" in tabs else {})  # fmt: skip
    picks, results = [], []
    for a in p.execute(select(alerts).order_by(alerts.c.created_at, alerts.c.alert_id)):
        pid = pick_id(a.dedup_key)
        if pid not in have_p:
            picks.append(pick_row(p, m, a, p_over_fn))
        if pid not in have_r:
            r = result_row(p, m, a, ag.get(a.alert_id), cg.get(a.alert_id), now)
            if r is not None:
                results.append(r)
    n_p = append_rows(out / "picks.csv", PICK_FIELDS, picks)
    n_r = append_rows(out / "results.csv", RESULT_FIELDS, results)
    labels = []
    for a in p.execute(select(alerts).order_by(alerts.c.created_at, alerts.c.alert_id)):
        lab = _details(a).get("label")
        if lab and lab.get("label"):
            labels.append({"pick_id": pick_id(a.dedup_key), "label": lab["label"],
                           "stake": _num(lab.get("stake")), "labeled_at": _iso(a.created_at),
                           "edge": _num(lab.get("edge")), "threshold": _num(lab.get("threshold")),
                           "edge_source": lab.get("edge_source") or "",
                           "pinnacle_sides": "" if lab.get("pinnacle_sides") is None
                           else str(bool(lab["pinnacle_sides"])).lower(),
                           "available": str(bool(lab.get("available"))).lower()})  # fmt: skip
    append_rows(out / "labels.csv", LABEL_FIELDS, labels)
    card_rows(p, out)
    annotate(out, now)
    return {"picks": n_p, "results": n_r, "milestones": milestones(out, now)}


def card_rows(p: Connection, out: Path) -> int:
    """One write-once row per pick on each chosen card day: card yes (label, stake) or no."""
    from betdata.alerts.card import day_picks
    from betdata.alerts.db import state

    if "poller_state" not in _tables(p):
        return 0
    rows = []
    for st in p.execute(select(state).where(state.c.key.like("card:%"))):
        c = st.value or {}
        if not c.get("day"):
            continue
        day = datetime.fromisoformat(c["day"]).date()
        chosen = {x["dedup_key"]: x for x in c.get("picks", [])}
        for a in day_picks(p, c["sport"], day):
            x = chosen.get(a.dedup_key)
            pin = None if x is None else x.get("pinnacle_sides")
            rows.append({"pick_id": pick_id(a.dedup_key), "sport": c["sport"],
                         "card_date": c["day"], "card": "yes" if x else "no",
                         "card_label": x["label"] if x else "",
                         "card_stake": _num(x["stake"]) if x else "",
                         "selected_at": c.get("selected_at", ""),
                         "ev": _num(x.get("ev")) if x else "",
                         "edge": _num(x.get("edge")) if x else "",
                         "threshold": _num(x.get("threshold")) if x else "",
                         "edge_source": (x.get("edge_source") or "") if x else "",
                         "pinnacle_sides": "" if pin is None else str(bool(pin)).lower()})
    return append_rows(out / "card.csv", CARD_FIELDS, rows)


def annotate(out: Path, now: datetime) -> int:
    """Append the standing annotations (module docstring) for every pick that lacks them."""
    rows = []
    for pk in read_rows(out / "picks.csv"):
        if pk["signal"] == "news" and pk["signal_name"] != LEGACY:
            rows.append(("signal_name", LEGACY, "M16 paper alert (pre-M18 rules)", pk))
        if pk["pick_id"] in M16_DUPLICATES:
            rows.append(("duplicate", "true", "repeat of an alerted change (REVIEW.md M16-4)",
                         pk))
    return append_rows(out / "annotations.csv", ANNOTATION_FIELDS,
                       [{"key": f"{pk['pick_id']}:{field}", "pick_id": pk["pick_id"],
                         "field": field, "value": value, "noted_at": _iso(now), "note": note}
                        for field, value, note, pk in rows], key="key")  # fmt: skip


# --- the record ----------------------------------------------------------------------------------


def joined(out: Path) -> list[dict]:
    """Every pick with its annotations laid over it and its result fields (empty until final)."""
    res = {r["pick_id"]: r for r in read_rows(out / "results.csv")}
    notes: dict[str, dict] = {}
    for n in read_rows(out / "annotations.csv"):
        notes.setdefault(n["pick_id"], {})[n["field"]] = n["value"]
    labs = {r["pick_id"]: r for r in read_rows(out / "labels.csv")}
    cards = {r["pick_id"]: r for r in read_rows(out / "card.csv")}
    rows = []
    for pk in read_rows(out / "picks.csv"):
        r = res.get(pk["pick_id"], {})
        lb = labs.get(pk["pick_id"], {})
        rows.append({**pk, "duplicate": "", **notes.get(pk["pick_id"], {}),
                     "label": lb.get("label", ""), "stake": lb.get("stake", ""),
                     **{f: cards.get(pk["pick_id"], {}).get(f, "")
                        for f in ("card", "card_label", "card_stake", "card_date")},
                     **{f: r.get(f, "") for f in RESULT_FIELDS if f != "pick_id"}})
    return rows


@dataclass
class Bank:
    """The simulated bankroll of the labelled picks (flat stakes, no compounding)."""

    picks: int = 0
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    units: float = 0.0  # stake x profit, summed over settled picks
    staked: float = 0.0  # stakes of settled picks (wins, losses, pushes)
    clv: list | None = None

    @property
    def bankroll(self) -> float:
        from betdata.alerts.labels import START_BANKROLL

        return START_BANKROLL + self.units

    @property
    def roi(self) -> float | None:
        return self.units / self.staked if self.staked else None


def bank(rows: list[dict], label: str = "label", stake: str = "stake") -> Bank:
    """The bankroll over the counted, labelled picks among `rows` (the card's: `card_label`,
    `card_stake`)."""
    b = Bank(clv=[])
    lab_key, stake_key = label, stake
    for x in counted(rows):
        if not x.get(lab_key):
            continue
        b.picks += 1
        stake = _f(x.get(stake_key)) or 0.0
        res = x.get("result", "")
        if res in ("win", "loss", "push"):
            b.wins += res == "win"
            b.losses += res == "loss"
            b.pushes += res == "push"
            b.units += stake * (_f(x.get("profit")) or 0.0)
            b.staked += stake
        if res != "void" and (c := _f(x.get("clv_consensus", ""))) is not None:
            b.clv.append(c)
    return b


def counted(rows: list[dict]) -> list[dict]:
    """The picks the record counts: not M16 legacy, not a duplicate."""
    return [r for r in rows if r["signal_name"] != LEGACY and r.get("duplicate") != "true"]


def _f(x: str) -> float | None:
    return None if x in ("", None) else float(x)


@dataclass
class Record:
    picks: int = 0
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    voids: int = 0
    units: float = 0.0
    clv: list | None = None
    clv_pin: list | None = None

    @property
    def settled(self) -> int:
        return self.wins + self.losses + self.pushes

    @property
    def roi(self) -> float | None:
        return self.units / self.settled if self.settled else None

    @property
    def graded(self) -> int:
        return self.settled

    def clv_stats(self, pin: bool = False) -> tuple[int, float | None, float | None, float | None]:
        from betdata.alerts.grade import mean_range

        xs = (self.clv_pin if pin else self.clv) or []
        mu, lo, hi = mean_range(xs)
        return len(xs), mu, lo, hi


def record(rows: list[dict]) -> Record:
    r = Record(clv=[], clv_pin=[])
    for x in rows:
        r.picks += 1
        res = x.get("result", "")
        if res == "win":
            r.wins += 1
        elif res == "loss":
            r.losses += 1
        elif res == "push":
            r.pushes += 1
        elif res == "void":
            r.voids += 1
        if res in ("win", "loss", "push"):
            r.units += _f(x.get("profit")) or 0.0
        if res != "void":
            if (c := _f(x.get("clv_consensus", ""))) is not None:
                r.clv.append(c)
            if (c := _f(x.get("clv_pinnacle", ""))) is not None:
                r.clv_pin.append(c)
    return r


def kind(row: dict) -> str:
    return "props" if row.get("player") else "game markets"


def breakdowns(rows: list[dict]) -> dict[str, dict[str, Record]]:
    out: dict[str, dict[str, Record]] = {"all": {"all picks": record(rows)}}
    for name, key in (("by sport", lambda r: r["sport"].upper()),
                      ("by signal", lambda r: r["signal_name"]),
                      ("props vs game markets", kind),
                      ("C signals by edge source",
                       lambda r: (r["edge_source"] or "untagged")
                       if r["signal"] == "value" else None)):  # fmt: skip
        groups: dict[str, list] = {}
        for r in rows:
            k = key(r)
            if k is not None:
                groups.setdefault(k, []).append(r)
        out[name] = {k: record(v) for k, v in sorted(groups.items())}
    return out


def milestones(out: Path, now: datetime) -> list[dict]:
    """Signals that have newly reached 60 or 200 graded picks (appended to milestones.csv,
    so each is announced once)."""
    path = out / "milestones.csv"
    done = {(r["signal_name"], r["graded"]) for r in read_rows(path)}
    by: dict[str, list] = {}
    for r in counted(joined(out)):
        by.setdefault(r["signal_name"], []).append(r)
    new = []
    for name, rs in sorted(by.items()):
        g = record(rs).graded
        for n in MILESTONES:
            if g >= n and (name, str(n)) not in done:
                new.append({"signal_name": name, "graded": str(n), "reached_at": _iso(now)})
    if new:
        _append_milestones(path, new)
    return new


def _append_milestones(path: Path, rows: list[dict]) -> None:
    keyed = [dict(r, key=f"{r['signal_name']}:{r['graded']}") for r in rows]
    old = read_rows(path)
    if old and "key" not in old[0]:
        raise AppendOnlyError(f"{path}: unexpected header")
    append_rows(path, (*MILESTONE_FIELDS, "key"), keyed, key="key")


# --- reload into a rebuilt poller database ------------------------------------------------------


def reload_alerts(pdb_conn: Connection, out: Path) -> int:
    """Re-insert every ledger pick missing from the poller's `alerts` (a rebuilt or lost
    database), so grading and the Value section continue. Returns picks reloaded."""
    from betdata.alerts.db import alerts

    have = set(pdb_conn.execute(select(alerts.c.dedup_key)).scalars())
    rows = []
    for pk in read_rows(out / "picks.csv"):
        if pk["dedup_key"] in have:
            continue
        rows.append(dict(
            signal=pk["signal"], sport=pk["sport"], game_id=int(pk["game_id"]),
            game_label=pk["game"] or None, tip=_dt(pk["tip"]), market=pk["market"],
            player_name=pk["player"] or None, side=pk["side"], line=_f(pk["line"]),
            price=int(pk["odds"]), book=pk["book"], fair_prob=_f(pk["fair_prob"]),
            book_prob=_f(pk["book_prob"]), reason=pk["reason"], dedup_key=pk["dedup_key"],
            created_at=_dt(pk["created_at"]), quiet=True, sent=False,
            send_status="reloaded from ledger",
            details={"reloaded_from_ledger": True, "edge_source": pk["edge_source"] or None,
                     "news_move": _f(pk["news_move"])}))  # fmt: skip
    if rows:
        pdb_conn.execute(insert(alerts), rows)
    return len(rows)


def _dt(s: str) -> datetime | None:
    return None if not s else datetime.fromisoformat(s)
