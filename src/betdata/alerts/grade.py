"""Grading alerts against the pre-game close (MODEL.md "M16 / Grading", issue #167).

`grade_all` runs at the start of every poller job (and `betdata grade alerts`). For each alert
whose game has started:

- **Close**: the last line stored strictly before tip from the alert's own book and market (and
  player, by normalized name), from the poller's `quotes` (SharpAPI / The Odds API) and the
  main database (`odds_snapshots` polls, full game; `prop_lines` polls), whichever is later.
  Signal 2 falls back to Pinnacle's last pre-game line when the soft book has none.
- **CLV points**: spreads `bet line - close line` (the side's own lines); overs `close - bet`,
  unders `bet - close`. None for the moneyline.
- **CLV no-vig** (probability, not % points): the close's no-vig probability of the bet side,
  carried to the bet line when the close moved (normal approximation, `SIGMA`), minus the
  book's no-vig probability for the side at alert time (`alerts.book_prob`).
- **Result and profit** (1 unit at the alerted price) once the game is final; `void` for a
  postponed or cancelled game or a player who did not play.

A grade is replaced when a later run knows more (a result after a close); unchanged grades are
not rewritten. The main database is only read.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import NormalDist

from sqlalchemy import Connection, Engine, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from betdata import moneyline as ml
from betdata.alerts.db import alert_grades, alerts, quotes
from betdata.alerts.poller import _norm, utc
from betdata.db.schema import (
    books,
    game_source_ids,
    games,
    markets,
    nba_player_games,
    odds_snapshots,
    player_game_snaps,
    prop_lines,
    results,
)

log = logging.getLogger(__name__)
# Fixed before any graded alert (MODEL.md, M16 grading implementation note, 2026-10-22).
SIGMA = {("nba", "spreads"): 12.0, ("nba", "totals"): 18.0, ("nfl", "spreads"): 13.5,
         ("nfl", "totals"): 13.0}  # fmt: skip
OTHER = {"home": "away", "away": "home", "over": "under", "under": "over"}
QUOTE_SOURCES = ("sharpapi", "the_odds_api")
VOID_STATUSES = ("postponed", "cancelled")
VOID = "void"
NBA_STAT = {"points": ("points",), "rebounds": ("oreb", "dreb"), "assists": ("ast",),
            "threes": ("fg3m",)}  # fmt: skip
NFL_STAT = {"rec_yds": "receiving_yards", "rush_yds": "rushing_yards",
            "receptions": "receptions"}  # fmt: skip
GAME_MARKETS = ("spreads", "totals", "h2h")
N = NormalDist()


@dataclass(frozen=True)
class Close:
    book: str
    line: float | None
    price: int | None
    other_price: int | None  # the other side at the same line, same source (for the no-vig)
    at: datetime


# --- the math (pure) ----------------------------------------------------------------------------


def sigma(sport: str, market: str, line: float | None) -> float | None:
    if market in GAME_MARKETS:
        return SIGMA.get((sport, market))
    return None if line is None else max(1.0, 0.5 * math.sqrt(max(line, 0.0)) + 0.15 * line)


def clv_points(market: str, side: str, bet: float | None, close: float | None) -> float | None:
    if market == "h2h" or bet is None or close is None:
        return None
    if market == "spreads" or side == "under":
        return round(bet - close, 2)
    return round(close - bet, 2)


def at_bet_line(p_close: float, points: float, sig: float) -> float:
    """The close's no-vig probability of the side carried to the bet line: the side's stat is
    N(mu, sig) with mu set so the close line gives `p_close`; a bet line `points` better (CLV
    points) moves the z-score by points / sig."""
    p = min(max(p_close, 1e-9), 1 - 1e-9)
    return N.cdf(N.inv_cdf(p) + points / sig)


def clv_novig(a, close: Close | None) -> float | None:
    if close is None or close.price is None or close.other_price is None:
        return None
    base = a.book_prob
    if base is None:  # the alert's own price pair, if it kept one
        pair = ml.no_vig(a.price, (a.details or {}).get("other_price"))
        base = pair[0] if pair else None
    nv = ml.no_vig(close.price, close.other_price)
    if base is None or nv is None:
        return None
    p, pts = nv[0], clv_points(a.market, a.side, a.line, close.line)
    if pts:
        sig = sigma(a.sport, a.market, close.line)
        if sig is None:
            return None
        p = at_bet_line(p, pts, sig)
    return round(p - base, 4)


def result_of(market: str, side: str, line: float | None, home: float, away: float) -> str:
    """A game market at final score (the side's own line for a spread)."""
    if market == "totals":
        cover = (home + away - line) * (1 if side == "over" else -1)
    else:
        cover = (home - away) * (1 if side == "home" else -1) + (line or 0.0)
    return "win" if cover > 0 else "loss" if cover < 0 else "push"


def prop_result(side: str, line: float, actual: float) -> str:
    diff = (actual - line) * (1 if side == "over" else -1)
    return "win" if diff > 0 else "loss" if diff < 0 else "push"


def latest_close(obs: list[tuple], side: str, market: str, book: str) -> Close | None:
    """obs: (store/source, at, outcome, line, price) rows before tip. Each source's latest row
    of the side is a candidate (paired with that source's latest row of the other side when
    the lines match); the latest candidate wins."""
    best = None
    for src in {o[0] for o in obs}:
        rows = sorted((o for o in obs if o[0] == src), key=lambda o: o[1])
        mine = [o for o in rows if o[2] == side and (o[3] is not None or market == "h2h")
                and (market != "h2h" or o[4] is not None)]  # fmt: skip
        if not mine:
            continue
        _, at, _, line, price = mine[-1]
        other = [o for o in rows if o[2] == OTHER[side]]
        want = None if line is None else -line if market == "spreads" else line
        op = other[-1][4] if other and other[-1][3] == want else None
        if best is None or at > best.at:
            best = Close(book, line, price, op, at)
    return best


# --- reading closes and results -----------------------------------------------------------------


def _poller_obs(p: Connection, a, book: str, tip: datetime) -> list[tuple]:
    q = quotes
    rows = p.execute(select(q.c.source, q.c.polled_at, q.c.outcome, q.c.line, q.c.price,
                            q.c.player_name)
                     .where(q.c.game_id == a.game_id, q.c.book == book, q.c.market == a.market,
                            q.c.source.in_(QUOTE_SOURCES), q.c.polled_at < tip,
                            q.c.price.is_not(None))).all()  # fmt: skip
    want = _norm(a.player_name) if a.player_name else None
    return [(("poller", r.source), utc(r.polled_at), r.outcome, r.line, r.price) for r in rows
            if (_norm(r.player_name) if r.player_name else None) == want]  # fmt: skip


def _main_obs(m: Connection, a, book: str, tip: datetime) -> list[tuple]:
    if a.player_name:
        pl = prop_lines
        rows = m.execute(select(pl.c.source, pl.c.fetched_at, pl.c.player_name, pl.c.line,
                                pl.c.over_price, pl.c.under_price)
                         .join(books, books.c.book_id == pl.c.book_id)
                         .where(pl.c.game_id == a.game_id, books.c.key == book,
                                pl.c.market == a.market, pl.c.kind == "poll",
                                pl.c.fetched_at < tip)).all()  # fmt: skip
        want = _norm(a.player_name)
        out = []
        for r in rows:
            if r.player_name and _norm(r.player_name) == want:
                at = utc(r.fetched_at)
                out += [(("main", r.source), at, "over", r.line, r.over_price),
                        (("main", r.source), at, "under", r.line, r.under_price)]  # fmt: skip
        return out
    s, gs = odds_snapshots, game_source_ids
    rows = m.execute(select(s.c.source, s.c.fetched_at, s.c.outcome, s.c.line,
                            s.c.price_american, gs.c.home_away_swapped)
                     .join(books, books.c.book_id == s.c.book_id)
                     .join(markets, markets.c.market_id == s.c.market_id)
                     .outerjoin(gs, (gs.c.source == s.c.source)
                                & (gs.c.source_game_id == s.c.source_game_id))
                     .where(s.c.game_id == a.game_id, books.c.key == book,
                            markets.c.key == a.market, markets.c.period == "game",
                            s.c.snapshot_kind == "poll", s.c.price_status == "active",
                            s.c.fetched_at < tip)).all()  # fmt: skip
    flip = {"home": "away", "away": "home"}
    return [(("main", r.source), utc(r.fetched_at),
             flip.get(r.outcome, r.outcome) if r.home_away_swapped else r.outcome, r.line,
             r.price_american) for r in rows]  # fmt: skip


def find_close(m: Connection, p: Connection, a, tip: datetime) -> Close | None:
    order = [a.book] + (["pinnacle"] if a.signal == "sharp" and a.book != "pinnacle" else [])
    for book in order:
        obs = _poller_obs(p, a, book, tip) + _main_obs(m, a, book, tip)
        close = latest_close(obs, a.side, a.market, book)
        if close is not None:
            return close
    return None


NflStats = Callable[[int], object]  # season -> nflverse player stats DataFrame


def _nfl_stats(season: int, data_dir=None):
    """The season's player stats: the raw nflverse file when it is on disk (hourly 7am run,
    dev), else the per-game box table the hourly state keeps in `data/derived`
    (`nfl_prop_box_<season>.parquet`, refreshed each morning). The poller job restores only the
    state, so it grades NFL props from the box table."""
    import pandas as pd

    from betdata.config import PROJECT_ROOT
    from betdata.nfl.nflverse_files import load_player_stats
    from betdata.nfl.props.data import STAT_COLS

    root = Path(data_dir) if data_dir else PROJECT_ROOT / "data"
    cols = ["player_display_name", "game_id", *NFL_STAT.values()]
    raw = root / "nflverse" / "stats_player" / f"stats_player_week_{season}.parquet"
    if raw.exists():
        return load_player_stats([season], columns=cols, data_dir=root)
    box = root / "derived" / f"nfl_prop_box_{season}.parquet"
    if not box.exists():
        raise FileNotFoundError(f"no NFL player stats for {season} ({raw.name} or {box.name})")
    back = {v: k for k, v in STAT_COLS.items()}
    b = pd.read_parquet(box).drop(columns=["game_id"], errors="ignore")  # our id; keep nflverse's
    b = b.rename(columns={"nid": "game_id", "name": "player_display_name", **back})
    return b[cols]


def prop_actual(m: Connection, a, nfl_stats: NflStats) -> float | str | None:
    """The player's stat, VOID if he did not play, None while the box score is not in."""
    want = _norm(a.player_name)
    if a.sport == "nba":
        rows = m.execute(select(nba_player_games).where(
            nba_player_games.c.game_id == a.game_id)).mappings().all()  # fmt: skip
        if not rows:
            return None
        r = next((r for r in rows if _norm(r["player_name"]) == want), None)
        if r is None or r["did_not_play"] or not (r["minutes"] or 0) > 0:
            return VOID
        return float(sum(r[c] or 0 for c in NBA_STAT[a.market]))
    g = m.execute(select(games.c.season, game_source_ids.c.source_game_id)
                  .join(game_source_ids, game_source_ids.c.game_id == games.c.game_id)
                  .where(games.c.game_id == a.game_id,
                         game_source_ids.c.source == "nflverse")).first()  # fmt: skip
    if g is None or a.market not in NFL_STAT:
        return None
    try:
        st = nfl_stats(g.season)
    except Exception as e:  # noqa: BLE001 - files not downloaded here: stays pending
        log.info("nfl player stats unavailable: %s", e)
        return None
    st = st[st.game_id == g.source_game_id]
    if st.empty:
        return None
    hit = st[[_norm(str(n)) == want for n in st.player_display_name]]
    if not hit.empty:
        return float(hit[NFL_STAT[a.market]].fillna(0).iloc[0])
    snaps = m.execute(select(player_game_snaps.c.player_name, player_game_snaps.c.offense_snaps)
                      .where(player_game_snaps.c.game_id == a.game_id)).all()  # fmt: skip
    if not snaps:
        return None
    played = any(_norm(r.player_name) == want and (r.offense_snaps or 0) > 0 for r in snaps)
    return 0.0 if played else VOID


def result(m: Connection, a, status: str | None, nfl_stats: NflStats) -> str | None:
    if status in VOID_STATUSES:
        return VOID
    if a.player_name:
        if status != "final" and not m.execute(select(results.c.game_id).where(
                results.c.game_id == a.game_id, results.c.final.is_(True))).first():  # fmt: skip
            return None
        val = prop_actual(m, a, nfl_stats)
        return val if val in (None, VOID) else prop_result(a.side, a.line, val)
    fin = m.execute(select(results.c.home_score, results.c.away_score)
                    .where(results.c.game_id == a.game_id, results.c.final.is_(True))
                    .order_by(results.c.source)).first()  # fmt: skip
    if fin is None or fin.home_score is None or fin.away_score is None:
        return None
    return result_of(a.market, a.side, a.line, fin.home_score, fin.away_score)


# --- the run ------------------------------------------------------------------------------


def grade_one(m: Connection, p: Connection, a, now: datetime,
              nfl_stats: NflStats = _nfl_stats) -> dict | None:  # fmt: skip
    g = m.execute(select(games.c.start_time_utc, games.c.status)
                  .where(games.c.game_id == a.game_id)).first()  # fmt: skip
    tip = utc(g.start_time_utc) if g else (utc(a.tip) if a.tip else None)
    if tip is None or tip >= now:
        return None
    close = find_close(m, p, a, tip)
    res = result(m, a, g.status if g else None, nfl_stats)
    return dict(close_line=close.line if close else None,
                close_price=close.price if close else None,
                close_book=close.book if close else None, close_at=close.at if close else None,
                clv_points=clv_points(a.market, a.side, a.line, close.line) if close else None,
                clv_novig=clv_novig(a, close), result=res,
                profit=None if res is None else round(ml.profit(res, a.price), 4))  # fmt: skip


def grade_all(main: Engine, pdb: Engine, now: datetime,
              nfl_stats: NflStats = _nfl_stats) -> int:  # fmt: skip
    """Grade every alert whose game has started; returns the number of grades written."""
    written = 0
    with pdb.begin() as p, main.connect() as m:
        have = {r.alert_id: r for r in p.execute(select(alert_grades))}
        for a in p.execute(select(alerts).where(alerts.c.tip < now)).all():
            old = have.get(a.alert_id)
            if old is not None and old.result is not None and old.close_at is not None:
                continue  # complete: a final result and a close
            vals = grade_one(m, p, a, now, nfl_stats)
            if vals is None:
                continue
            if old is not None and all(getattr(old, k) == v for k, v in vals.items()):
                continue
            if old is None:
                p.execute(sqlite_insert(alert_grades).values(alert_id=a.alert_id, graded_at=now,
                                                             **vals))  # fmt: skip
            else:
                p.execute(update(alert_grades).where(alert_grades.c.alert_id == a.alert_id)
                          .values(graded_at=now, **vals))  # fmt: skip
            written += 1
    log.info("alert grading: %s grades written", written)
    return written


# --- reports (MODEL.md "Reports" and "Success rule") -------------------------------------------

VERDICT_N = 200  # graded alerts per signal before any verdict


def mean_range(xs: list[float]) -> tuple[float | None, float | None, float | None]:
    """Mean and its 95% range, mean +- 1.96 sd / sqrt(n) (sample sd; no range for n < 2)."""
    if not xs:
        return None, None, None
    mu = sum(xs) / len(xs)
    if len(xs) < 2:
        return mu, None, None
    half = 1.96 * math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)) / math.sqrt(len(xs))
    return mu, mu - half, mu + half


def group_stats(rows) -> dict:
    """rows: graded alerts (mappings with clv_points, clv_novig, result, profit)."""
    pts = [r["clv_points"] for r in rows if r["clv_points"] is not None]
    nv = [r["clv_novig"] for r in rows if r["clv_novig"] is not None]
    bets = [r for r in rows if r["result"] in ("win", "loss", "push")]
    mu, lo, hi = mean_range(nv)
    return {"n": len(rows), "n_points": len(pts), "clv_points": sum(pts) / len(pts) if pts
            else None, "n_novig": len(nv), "clv_novig": mu, "lo": lo, "hi": hi,
            "settled": len(bets), "wins": sum(r["result"] == "win" for r in bets),
            "losses": sum(r["result"] == "loss" for r in bets),
            "roi": sum(r["profit"] for r in bets) / len(bets) if bets else None}  # fmt: skip


def verdict(s: dict) -> str:
    """The success rule: no verdict before 200 graded (no-vig CLV) alerts; then "working" only
    if the average no-vig CLV is positive and its 95% range excludes zero."""
    if s["n_novig"] < VERDICT_N:
        return f"no verdict before {VERDICT_N} graded ({s['n_novig']} so far)"
    return "working" if s["clv_novig"] > 0 and s["lo"] > 0 else "not working"


def graded_rows(p: Connection) -> list:
    return p.execute(select(alerts, alert_grades.c.clv_points, alert_grades.c.clv_novig,
                            alert_grades.c.result, alert_grades.c.profit,
                            alert_grades.c.close_line, alert_grades.c.close_book)
                     .join(alert_grades, alert_grades.c.alert_id == alerts.c.alert_id)
                     ).mappings().all()  # fmt: skip


def _f(x, fmt: str, scale: float = 1.0) -> str:
    return "-" if x is None else format(scale * x, fmt)


def report_lines(p: Connection) -> list[str]:
    rows = graded_rows(p)
    if not rows:
        from betdata.alerts.gates import gates

        return ["no graded alerts yet"] + [f"M18 {k}: {v['alerts']} alerts, {v['status']}"
                                           for k, v in gates(p).items()]
    out = ["CLV no-vig in % points (95% range); CLV points for spreads, totals and props; "
           "ROI per 1-unit paper bet at the alerted price (voids excluded)",
           f"{'signal':6} {'sport':5} {'market':10} {'n':>4} {'CLV pts':>8} {'n nv':>5} "
           f"{'CLV no-vig':>10} {'95% range':>17} {'W-L':>7} {'ROI':>7}"]  # fmt: skip
    groups: dict = {}
    for r in rows:
        groups.setdefault((r["signal"], r["sport"], r["market"]), []).append(r)
    for (sig, sport, market), rs in sorted(groups.items()):
        s = group_stats(rs)
        rng = "-" if s["lo"] is None else f"[{100 * s['lo']:+.2f}, {100 * s['hi']:+.2f}]"
        wl = f"{s['wins']}-{s['losses']}"
        out.append(f"{sig:6} {sport:5} {market:10} {s['n']:4} {_f(s['clv_points'], '+.2f'):>8} "
                   f"{s['n_novig']:5} {_f(s['clv_novig'], '+.2f', 100):>10} {rng:>17} "
                   f"{wl:>7} {_f(s['roi'], '+.1%'):>7}")  # fmt: skip
    for sig in sorted({r["signal"] for r in rows}):
        s = group_stats([r for r in rows if r["signal"] == sig])
        out.append(f"{sig}: {verdict(s)}")
    from betdata.alerts.gates import gates

    for name, g in gates(p).items():  # M18 Part H: CLV against the consensus close
        rng = "-" if g["lo"] is None else f"[{100 * g['lo']:+.2f}, {100 * g['hi']:+.2f}]"
        out.append(f"M18 {name}: {g['alerts']} alerts, {g['n']} graded vs the consensus close, "
                   f"CLV {_f(g['clv'], '+.2f', 100)} pp {rng}, {g['wins']}-{g['losses']}, "
                   f"ROI {_f(g['roi'], '+.1%')}: {g['status']}")
    return out
