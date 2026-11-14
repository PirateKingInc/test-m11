"""Game-market grades for BOTH sides at two snapshots (owner 2026-11-14, PROJECT.md "Game
grades"; `ledger/engine_game_grades.csv`, append-only; `engine_grades.csv` is never rewritten).

For every final NBA game, for h2h and spreads, for the home and the away side, and for two
snapshots of the engine's document:
- `morning`: the 6 AM ET hour's engine run (the 6:15 AM morning update), kept write-once per game
  in `data/derived/nba_engine_snapshots_morning/`;
- `pretip`: the existing last pre-tip document (`nba_engine_snapshots/`, ~30 minutes before tip);
one row with the DraftKings (else FanDuel) line and price of that side as stored at or before the
snapshot time (the line the pick would have been made at), the book's no-vig probability, the
engine's probability of the side at that line (no push), and the result. The away side's
probability is 1 - the home side's at the same line (the spread's own line for the away side is
minus the home line). Built from stored engine runs and stored lines only (no extra calls).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

log = logging.getLogger(__name__)
FILE = "engine_game_grades.csv"
MORNING_DIR = Path("data/derived/nba_engine_snapshots_morning")
PRETIP_DIR = Path("data/derived/nba_engine_snapshots")
FIELDS = ("key", "game_id", "game", "tip", "snapshot", "snapshot_at", "market", "side", "line",
          "price", "other_price", "book", "p_engine", "p_novig", "result", "y", "graded_at",
          "version")  # fmt: skip
MARKETS = ("h2h", "spreads")
BOOKS = ("draftkings", "fanduel")
MORNING_HOUR = 6  # Eastern: the morning update's engine run
KEEP = timedelta(days=7)


def morning_snapshot(doc: dict, now: datetime, snap_dir: Path = MORNING_DIR) -> int:
    """In the 6 AM ET hour (the morning update), keep each upcoming game's engine document once
    (the first morning run's)."""
    from betdata.runner.window import EASTERN

    if now.astimezone(EASTERN).hour != MORNING_HOUR:
        return 0
    snap_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for g in doc.get("games", []):
        f = snap_dir / f"{int(g['game_id'])}.json"
        if datetime.fromisoformat(g["tip"]) <= now or f.exists():
            continue
        f.write_text(json.dumps({**g, "snapshot_at": doc.get("generated_at") or now.isoformat(),
                                 "version": doc.get("version")}, separators=(",", ":")))
        n += 1
    for f in snap_dir.glob("*.json"):
        try:
            tip = datetime.fromisoformat(json.loads(f.read_text())["tip"])
        except Exception:  # noqa: BLE001
            f.unlink(missing_ok=True)
            continue
        if now - tip > KEEP:
            f.unlink(missing_ok=True)
    return n


def quote_at(p, m, gid: int, market: str, at: datetime):
    """The home side's DK (else FD) two-way quote as stored at or before `at`: a Close with
    (line, price, other_price, book), or None."""
    from betdata.alerts.grade import _main_obs, _poller_obs, latest_close
    from betdata.nba.engine.picks import real_two_way

    a = SimpleNamespace(game_id=gid, market=market, player_name=None)
    for book in BOOKS:
        obs = (_poller_obs(p, a, book, at) if p is not None else []) + (
            _main_obs(m, a, book, at) if m is not None else [])
        c = latest_close(obs, "home", market, book)
        if (c is not None and c.price is not None and c.other_price is not None
                and (c.line is not None or market == "h2h")
                and real_two_way(c.price, c.other_price)):
            return c
    return None


def side_rows(snap: dict, kind: str, c, market: str, home: float, away: float) -> list[dict]:
    """The home and away rows of one market at one snapshot (pure)."""
    from betdata.alerts.grade import result_of
    from betdata.nba.engine.picks import _engine_side, no_vig

    ph = _engine_side(snap, market, c.line)
    if ph is None:
        return []
    nv = no_vig(c.price, c.other_price)
    out = []
    for side, p, price, other, line in (
            ("home", ph, c.price, c.other_price, c.line),
            ("away", 1 - ph, c.other_price, c.price,
             None if c.line is None else -c.line)):  # fmt: skip
        res = result_of(market, side, line, home, away)
        out.append({"key": f"{int(snap['game_id'])}:{market}:{side}:{kind}", "snapshot": kind,
                    "snapshot_at": snap.get("snapshot_at", ""), "market": market, "side": side,
                    "line": "" if line is None else line, "price": price, "other_price": other,
                    "book": c.book, "p_engine": round(p, 5),
                    "p_novig": round(nv if side == "home" else 1 - nv, 5), "result": res,
                    "y": {"win": "1", "loss": "0"}.get(res, "")})  # fmt: skip
    return out


def game_rows(p, m, snaps: dict, now: datetime) -> list[dict] | None:
    """Every row of one final game (`snaps`: kind -> snapshot document), or None while the
    game is not final."""
    from betdata.alerts.grade import utc
    from betdata.nba.engine_grades import _final

    any_snap = next(iter(snaps.values()))
    gid = int(any_snap["game_id"])
    fin = _final(m, gid)
    if fin is None:
        return None
    home, away = fin
    tip = utc(datetime.fromisoformat(any_snap["tip"]))
    rows = []
    for kind, snap in snaps.items():
        at = utc(datetime.fromisoformat(snap.get("snapshot_at") or snap["tip"]))
        at = min(at, tip)
        base = {"game_id": gid, "game": snap.get("label", ""), "tip": tip.isoformat(),
                "graded_at": now.isoformat(), "version": snap.get("version") or ""}
        for market in MARKETS:
            c = quote_at(p, m, gid, market, at + timedelta(seconds=1))
            if c is None:
                continue
            rows += [{**base, **r} for r in side_rows(snap, kind, c, market, home, away)]
    return rows


def grade(p, m, out: Path, now: datetime, morning_dir: Path = MORNING_DIR,
          pretip_dir: Path = PRETIP_DIR) -> int:  # fmt: skip
    """Append the rows of every newly final game with a snapshot. Returns rows appended."""
    from betdata.ledger import append_rows, read_rows

    done = {r["game_id"] for r in read_rows(out / FILE)}
    by_game: dict = {}
    for kind, d in (("morning", morning_dir), ("pretip", pretip_dir)):
        if not d.exists():
            continue
        for f in d.glob("*.json"):
            try:
                s = json.loads(f.read_text())
            except Exception:  # noqa: BLE001
                continue
            if str(int(s["game_id"])) in done:
                continue
            by_game.setdefault(int(s["game_id"]), {})[kind] = s
    rows: list[dict] = []
    for _gid, snaps in sorted(by_game.items()):
        try:
            r = game_rows(p, m, snaps, now)
        except Exception as e:  # noqa: BLE001 - one game never stops the others
            log.warning("engine game grades %s failed: %s", _gid, e)
            continue
        if r:
            rows += r
    return append_rows(out / FILE, FIELDS, rows, key="key")
