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
            "signal_name": signal_name(a.signal, a.sport), "edge_source": tag or "",
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
    return {"picks": n_p, "results": n_r, "milestones": milestones(out, now)}


# --- the record ----------------------------------------------------------------------------------


def joined(out: Path) -> list[dict]:
    """Every pick with its result fields (empty until final)."""
    res = {r["pick_id"]: r for r in read_rows(out / "results.csv")}
    rows = []
    for pk in read_rows(out / "picks.csv"):
        r = res.get(pk["pick_id"], {})
        rows.append({**pk, **{f: r.get(f, "") for f in RESULT_FIELDS if f != "pick_id"}})
    return rows


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
    for r in joined(out):
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
