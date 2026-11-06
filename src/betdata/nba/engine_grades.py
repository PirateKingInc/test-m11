"""Every NBA engine prediction graded, not only picks (owner override 2026-11-04, MODEL.md "Engine
card: every prediction graded").

- `snapshot`: each hourly run keeps, per upcoming game, the engine's latest pre-tip document
  (`data/derived/nba_engine_snapshots/<game_id>.json`, overwritten until the tip, so the one
  kept is the last before it).
- `grade_games`: once a game is final (and its box score is in), for every game market (spread,
  total, moneyline) and every projected player prop (points, rebounds, assists, threes, PRA) with
  a stored DraftKings or FanDuel closing line (DraftKings first), one row: the engine's
  probability of the home side / the over at the closing line (no push), the closing no-vig
  probability of the same side, and the result. Appended once to `ledger/engine_grades.csv`
  (append-only, keyed by game, market and player).
- `summary`: calibration (expected calibration error over ten probability bins) and the Brier
  score of the engine against the close's, with a 95% range on the paired difference: for the
  Research tab and the weekly report, so the engine's accuracy is known within weeks.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

log = logging.getLogger(__name__)
SNAP_DIR = Path("data/derived/nba_engine_snapshots")
FILE = "engine_grades.csv"
FIELDS = ("key", "game_id", "game", "tip", "kind", "market", "player", "side", "line", "book",
          "p_engine", "p_close", "result", "y", "snapshot_at", "graded_at", "version")
BOOK_ORDER = ("draftkings", "fanduel")
GAME_MARKETS = ("spreads", "totals", "h2h")
KEEP = timedelta(days=7)  # snapshots of graded (or abandoned) games are pruned after this


# --- snapshots (hourly) ------------------------------------------------------------------------


def snapshot(doc: dict, now: datetime, snap_dir: Path = SNAP_DIR) -> int:
    """Keep each upcoming game's latest pre-tip engine document. Returns games written."""
    snap_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for g in doc.get("games", []):
        if datetime.fromisoformat(g["tip"]) <= now:
            continue
        rec = {**g, "snapshot_at": doc.get("generated_at") or now.isoformat(),
               "version": doc.get("version")}
        (snap_dir / f"{int(g['game_id'])}.json").write_text(json.dumps(rec,
                                                                       separators=(",", ":")))
        n += 1
    for f in snap_dir.glob("*.json"):  # prune old snapshots
        try:
            tip = datetime.fromisoformat(json.loads(f.read_text())["tip"])
        except Exception:  # noqa: BLE001 - a broken file is dropped
            f.unlink(missing_ok=True)
            continue
        if now - tip > KEEP:
            f.unlink(missing_ok=True)
    return n


# --- grading (nightly, in the hourly run's ledger step) ----------------------------------------


def _no_vig(a: int, b: int) -> float:
    from betdata.nba.engine.picks import no_vig

    return no_vig(a, b)


def _close(p, m, gid: int, market: str, player: str | None, side: str, tip: datetime):
    """The closing (line, price, other price, book) of the side: DraftKings, else FanDuel."""
    from betdata.alerts.grade import _main_obs, _poller_obs, latest_close
    from betdata.nba.engine.picks import real_two_way

    a = SimpleNamespace(game_id=gid, market=market, player_name=player)
    for book in BOOK_ORDER:
        obs = (_poller_obs(p, a, book, tip) if p is not None else []) + (
            _main_obs(m, a, book, tip) if m is not None else [])
        c = latest_close(obs, side, market, book)
        if (c is not None and c.price is not None and c.other_price is not None
                and (c.line is not None or market == "h2h")
                and real_two_way(c.price, c.other_price)):
            return c
    return None


def _prop_pairs(p, m, gid: int, tip: datetime) -> set[tuple[str, str]]:
    """(market, player name) of every prop with a stored DK/FD line before the tip."""
    from sqlalchemy import select

    from betdata.alerts.db import quotes
    from betdata.nba.engine.picks import PROP_MARKETS

    out: set = set()
    if p is not None:
        q = quotes
        out |= {(r.market, r.player_name) for r in p.execute(
            select(q.c.market, q.c.player_name).distinct().where(
                q.c.game_id == gid, q.c.book.in_(BOOK_ORDER), q.c.player_name.is_not(None),
                q.c.market.in_(tuple(PROP_MARKETS)), q.c.polled_at < tip))}  # fmt: skip
    if m is not None:
        from betdata.db.schema import books, prop_lines

        pl = prop_lines
        out |= {(r.market, r.player_name) for r in m.execute(
            select(pl.c.market, pl.c.player_name).distinct()
            .join(books, books.c.book_id == pl.c.book_id)
            .where(pl.c.game_id == gid, books.c.key.in_(BOOK_ORDER),
                   pl.c.market.in_(tuple(PROP_MARKETS)), pl.c.fetched_at < tip))}  # fmt: skip
    return out


def _final(m, gid: int) -> tuple[float, float] | None:
    """The final score (the results table the grading uses), or None while not final."""
    from betdata.alerts.tracker import _final_score

    sc = _final_score(m, gid)
    return None if sc is None else (float(sc[0]), float(sc[1]))


def _y(result: str) -> str:
    return {"win": "1", "loss": "0"}.get(result, "")


def game_rows(p, m, snap: dict, now: datetime) -> list[dict] | None:
    """The graded rows of one final game, or None while it is not final / its box is not in."""
    from betdata.alerts.grade import VOID, prop_actual, prop_result, result_of, utc
    from betdata.nba.engine.live import p_over
    from betdata.nba.engine.picks import PROP_MARKETS, _engine_side

    gid, tip = int(snap["game_id"]), utc(datetime.fromisoformat(snap["tip"]))
    fin = _final(m, gid)
    if fin is None:
        return None
    home, away = fin
    from sqlalchemy import select

    from betdata.db.schema import nba_player_games

    if m.execute(select(nba_player_games.c.game_id)
                 .where(nba_player_games.c.game_id == gid).limit(1)).first() is None:
        return None  # the box score is not in yet: grade the game on a later run
    base = {"game_id": gid, "game": snap.get("label", ""), "tip": tip.isoformat(),
            "snapshot_at": snap.get("snapshot_at", ""), "graded_at": now.isoformat(),
            "version": snap.get("version") or ""}
    rows = []
    for market in GAME_MARKETS:
        side = "over" if market == "totals" else "home"
        c = _close(p, m, gid, market, None, side, tip)
        if c is None:
            continue
        pe = _engine_side(snap, market, c.line)
        if pe is None:
            continue
        res = result_of(market, side, c.line, home, away)
        rows.append({**base, "key": f"{gid}:{market}:", "kind": "game", "market": market,
                     "player": "", "side": side, "line": "" if c.line is None else c.line,
                     "book": c.book, "p_engine": round(pe, 5),
                     "p_close": round(_no_vig(c.price, c.other_price), 5), "result": res,
                     "y": _y(res)})  # fmt: skip
    from betdata.alerts.poller import _norm

    by_name = {_norm(pl["name"]): pl for pl in snap.get("players", []) if pl.get("name")}
    pairs = sorted(_prop_pairs(p, m, gid, tip))
    for market, name in pairs:
        pl = by_name.get(_norm(name))
        stat = PROP_MARKETS.get(market)
        if pl is None or stat is None or not pl.get("cdf", {}).get(stat):
            continue  # not projected by the engine
        c = _close(p, m, gid, market, name, "over", tip)
        if c is None or c.line is None:
            continue
        r = p_over(pl["cdf"][stat], c.line)
        if r is None:
            continue
        pe = r[0] / max(1 - r[1], 1e-9)
        act = prop_actual(m, SimpleNamespace(sport="nba", game_id=gid, player_name=pl["name"],
                                             market=market), None)  # fmt: skip
        if act is None:  # the box score is not in yet: grade the game on a later run
            return None
        res = VOID if act == VOID else prop_result("over", c.line, float(act))
        rows.append({**base, "key": f"{gid}:{market}:{pl['name'].lower()}", "kind": "prop",
                     "market": market, "player": pl["name"], "side": "over", "line": c.line,
                     "book": c.book, "p_engine": round(pe, 5),
                     "p_close": round(_no_vig(c.price, c.other_price), 5), "result": res,
                     "y": _y(res)})  # fmt: skip
    return rows


def grade_games(p, m, out: Path, now: datetime, snap_dir: Path = SNAP_DIR) -> int:
    """Append the rows of every newly final snapshotted game. Returns rows appended."""
    from betdata.ledger import append_rows, read_rows

    if not snap_dir.exists():
        return 0
    done = {r["game_id"] for r in read_rows(out / FILE)}
    rows: list[dict] = []
    for f in sorted(snap_dir.glob("*.json")):
        snap = json.loads(f.read_text())
        if str(int(snap["game_id"])) in done:
            continue
        try:
            got = game_rows(p, m, snap, now)
        except Exception as e:  # noqa: BLE001 - one game never stops the others
            log.warning("engine grade %s failed: %s", snap.get("game_id"), e)
            continue
        if got:
            rows += got
    return append_rows(out / FILE, FIELDS, rows, key="key")


# --- the summary --------------------------------------------------------------------------------

BINS10 = tuple(i / 10 for i in range(11))


@dataclass
class Summary:
    n: int
    brier_engine: float | None
    brier_close: float | None
    diff: tuple  # (mean, low, high) of engine - close squared error, per prediction
    ece_engine: float | None
    ece_close: float | None
    table: list

    @property
    def verdict(self) -> str:
        m, lo, hi = self.diff
        if m is None:
            return "no graded predictions yet"
        if hi is not None and hi < 0:
            return "engine beats the close"
        if lo is not None and lo > 0:
            return "the close beats the engine"
        return "no clear difference yet"


def _ece(ps: list[float], ys: list[int]) -> tuple[float | None, list]:
    n, ece, table = len(ps), 0.0, []
    for lo, hi in zip(BINS10, BINS10[1:], strict=False):
        idx = [i for i, p in enumerate(ps) if lo <= p < hi or (hi == 1.0 and p == 1.0)]
        if not idx:
            continue
        mp = sum(ps[i] for i in idx) / len(idx)
        wr = sum(ys[i] for i in idx) / len(idx)
        table.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(idx), "predicted": round(mp, 4),
                      "actual": round(wr, 4)})
        ece += len(idx) / n * abs(mp - wr)
    return (ece if n else None), table


def summary(rows: list[dict], kind: str | None = None) -> Summary:
    from betdata.checks import mean_ci

    rs = [r for r in rows if r.get("y") in ("0", "1") and (kind is None or r["kind"] == kind)]
    pe = [float(r["p_engine"]) for r in rs]
    pc = [float(r["p_close"]) for r in rs]
    ys = [int(r["y"]) for r in rs]
    n = len(rs)
    if not n:
        return Summary(0, None, None, (None, None, None), None, None, [])
    be = sum((p - y) ** 2 for p, y in zip(pe, ys, strict=True)) / n
    bc = sum((p - y) ** 2 for p, y in zip(pc, ys, strict=True)) / n
    d = mean_ci([(a - y) ** 2 - (c - y) ** 2 for a, c, y in zip(pe, pc, ys, strict=True)])
    ece_e, table = _ece(pe, ys)
    ece_c, _ = _ece(pc, ys)
    return Summary(n, be, bc, d, ece_e, ece_c, table)


def summary_line(s: Summary) -> str:
    """'412 graded · Brier engine 0.2391 vs close 0.2377 (diff +0.0014, 95% -0.0010 to +0.0038)
    · ECE engine 0.031, close 0.018 · no clear difference yet'."""
    if not s.n:
        return "No graded predictions yet (the first games grade the morning after they end)."

    def f(x):
        return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.4f}"

    m, lo, hi = s.diff
    rng = "" if lo is None else f", 95% {lo:+.4f} to {hi:+.4f}"
    return (f"{s.n} graded · Brier engine {f(s.brier_engine)} vs close {f(s.brier_close)} "
            f"(diff {m:+.4f}{rng}) · ECE engine {f(s.ece_engine)}, close {f(s.ece_close)} · "
            f"{s.verdict}")


def read(out: Path) -> list[dict]:
    from betdata.ledger import read_rows

    return read_rows(out / FILE)


# --- the parlay bars' calibration check (owner 2026-11-06; reporting only) ---------------------

BANDS = ((0.65, 0.70, "65-70%"), (0.70, 0.80, "70-80%"), (0.80, 1.0001, "80%+"))


def band_table(rows: list[dict], kind: str | None = None) -> list[dict]:
    """How often the engine's 65-70%, 70-80% and 80%+ predictions hit: each graded prediction
    counted on the side the engine favours (p or 1 - p), pushes and voids left out. Reporting
    only: the parlay's bars are never changed from it."""
    out = []
    pts = []
    for r in rows:
        if r.get("y") not in ("0", "1") or (kind and r["kind"] != kind):
            continue
        p, y = float(r["p_engine"]), int(r["y"])
        pts.append((p, y) if p >= 0.5 else (1 - p, 1 - y))
    for lo, hi, name in BANDS:
        sel = [(p, y) for p, y in pts if lo <= p < hi]
        n = len(sel)
        out.append({"band": name, "n": n,
                    "predicted": None if not n else round(sum(p for p, _ in sel) / n, 4),
                    "hit": None if not n else round(sum(y for _, y in sel) / n, 4)})
    return out


def band_line(t: list[dict]) -> str:
    return " · ".join(f"{b['band']}: " + ("no predictions yet" if not b["n"] else
                      f"{100 * b['hit']:.1f}% hit vs {100 * b['predicted']:.1f}% predicted "
                      f"({b['n']})") for b in t)  # fmt: skip
