"""The daily Next Gen parlay (owner decision 2026-11-06, MODEL.md "Next Gen parlay"). Paper picks
only; no change to the two cards, models, pick rules or the grading of existing picks.

**Selection** (at 6:15 AM ET with the cards, betdata/morning.py), from Next Gen's evaluation of
every priced outcome of today's NBA games (every engine candidate, not only its picks):
- a leg: the value floor, Next Gen's blended probability (the fair probability its pick rule
  uses) at or above the market's no-vig probability; price between -200 and +100; at most one
  leg per game;
- agreement (owner 2026-11-09): the engine's own raw (unblended) probability >= 70% for the
  3-leg candidate (default), >= 65% for the 2-leg candidate;
- each candidate is priced entirely at one book (DraftKings or FanDuel, all legs there),
  whichever gives the better EV; parlay EV = product of the legs' blended probabilities x the
  parlay's decimal odds - 1 (conservative); at one book the best combination is the top legs
  by probability x decimal, one per game;
- the best 3-leg is chosen; the best 2-leg only if its EV is at least 3 points higher (or there
  is no 3-leg); neither: no parlay today ("No parlay today: no legs met the bar").
Chosen once per day (poller state `parlay:nba:<day>`), written once to `ledger/parlays.csv`
(signal `ng-parlay`).

**Grading** (`grade`): 0.25 units. It wins only if every leg wins. A pushed or void leg drops
out and the parlay is re-priced at the remaining legs' odds; with fewer than 2 legs left it
grades as the remaining single leg, or void. Results: `ledger/parlay_results.csv`, once.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

SIGNAL = "ng-parlay"
STAKE = 0.25
BOOKS = ("draftkings", "fanduel")
MIN_PRICE, MAX_PRICE = -200, 100
BARS = {3: 0.70, 2: 0.65}
SWITCH_PP = 0.03  # the 2-leg replaces the 3-leg only with EV at least 3 points higher
NONE_TEXT = "No parlay today: no legs met the bar"
FILE, RESULTS = "parlays.csv", "parlay_results.csv"
FIELDS = ("parlay_id", "day", "signal", "chosen_at", "kind", "book", "decimal", "american",
          "ev", "p_hit", "stake", "legs", "note")  # fmt: skip
RESULT_FIELDS = ("parlay_id", "day", "result", "profit", "units", "legs_left", "decimal_paid",
                 "leg_results", "graded_at")  # fmt: skip


def decimal(american: int) -> float:
    return 1 + american / 100 if american > 0 else 1 + 100 / abs(american)


def american(dec: float) -> int:
    return round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))


@dataclass
class Leg:
    game_id: int
    game: str  # "ORL @ MIA"
    tip: str  # ISO UTC
    market: str
    side: str
    line: float | None
    player: str | None
    price: int
    book: str
    p: float  # Next Gen's blended probability (fair): the value floor and the EV
    p_market: float  # the market's no-vig probability
    who: str = ""  # "Bam Adebayo (MIA) vs ORL", "MIA vs ORL", "Game total"
    p_raw: float | None = None  # the engine's own raw probability
    p_cal: float | None = None  # the raw probability after the weekly calibration fit (rule h):
    # the agreement bar since 2026-11-10 (calibration.py)

    @property
    def dec(self) -> float:
        return decimal(self.price)


@dataclass
class Parlay:
    kind: int
    book: str
    legs: list[Leg] = field(default_factory=list)

    @property
    def decimal(self) -> float:
        return math.prod(x.dec for x in self.legs)

    @property
    def p_hit(self) -> float:
        return math.prod(x.p for x in self.legs)

    @property
    def ev(self) -> float:
        return self.p_hit * self.decimal - 1


def qualifies(c, bar: float, cal=None) -> bool:
    """A leg: the blended probability at or above the market's no-vig (the value floor), price
    in [-200, +100], and the engine's probability (calibrated by `cal` when given) at or above
    the agreement bar."""
    agree = c.p_engine if cal is None else cal(c.p_engine)
    return (c.book in BOOKS and MIN_PRICE <= c.price <= MAX_PRICE and c.fair >= c.p_market
            and agree >= bar)  # fmt: skip


def raw(x: Leg) -> float:
    """The agreement probability: the calibrated engine probability when a fit exists (rule
    h, 2026-11-10), else the engine's raw probability, else the blend."""
    if x.p_cal is not None:
        return x.p_cal
    return x.p if x.p_raw is None else x.p_raw


def best_at_book(legs: list[Leg], k: int) -> Parlay | None:
    """The best k-leg parlay at one book, one leg per game: each game's best leg by
    probability x decimal, then the top k games (the product is largest that way)."""
    per_game: dict[int, Leg] = {}
    for x in legs:
        cur = per_game.get(x.game_id)
        if cur is None or x.p * x.dec > cur.p * cur.dec:
            per_game[x.game_id] = x
    if len(per_game) < k:
        return None
    top = sorted(per_game.values(), key=lambda x: -(x.p * x.dec))[:k]
    return Parlay(k, top[0].book, sorted(top, key=lambda x: (x.tip, x.game)))


def best(legs: list[Leg], k: int) -> Parlay | None:
    """The best k-leg parlay priced entirely at DraftKings or FanDuel (better EV wins)."""
    bar = BARS[k]
    out = None
    for book in BOOKS:
        pool = [x for x in legs if x.book == book and raw(x) >= bar]
        cand = best_at_book(pool, k)
        if cand is not None and (out is None or cand.ev > out.ev + 1e-12):
            out = cand
    return out


def choose(legs: list[Leg]) -> Parlay | None:
    """The best 3-leg; the best 2-leg only if its EV is >= 3 points higher (or no 3-leg)."""
    three, two = best(legs, 3), best(legs, 2)
    if three is None:
        return two
    if two is not None and two.ev >= three.ev + SWITCH_PP - 1e-12:
        return two
    return three


def leg_of(c, game_id: int, label: str, tip: datetime, who: str, cal=None) -> Leg:
    return Leg(game_id, label, tip.isoformat(), c.market, c.side, c.line, c.player,
               int(c.price), c.book, float(c.fair), float(c.p_market), who,
               float(c.p_engine),
               None if cal is None else round(cal(float(c.p_engine)), 4))  # fmt: skip


def _who(c, game: dict, label: str) -> str:
    aw, _, hm = (label or "? @ ?").partition(" @ ")
    if c.player:
        pl = next((x for x in game.get("players", []) if x.get("name") == c.player), {})
        team, opp = (hm, aw) if pl.get("is_home") else (aw, hm)
        return f"{c.player} ({team}) vs {opp}" if pl else c.player
    if c.market == "totals":
        return f"{aw} @ {hm} total"
    team, opp = (hm, aw) if c.side == "home" else (aw, hm)
    return f"{team} vs {opp}"


def candidate_legs(poller, main, now: datetime) -> list[Leg]:
    """Every priced outcome Next Gen evaluates for today's games (archived DK/FD lines)."""
    from betdata import eligibility as EL
    from betdata.alerts.rules import EASTERN
    from betdata.calibration import current
    from betdata.ledger import ledger_dir
    from betdata.morning import stored_quotes, today_games
    from betdata.nba.engine import live as L
    from betdata.nba.engine import picks as K

    if not L.FIT_PATH.exists():
        return []
    design = K.Design.from_json(L.load_fit()[1]["design"])
    cal = current(ledger_dir(), now.astimezone(EASTERN).date())  # rule h
    legs = []
    for gid in today_games(main, "nba", now):
        game = poller.engine_game(gid, now, False)
        if game is None:
            continue
        with poller.pdb.connect() as p:
            qs = stored_quotes(p, gid, now)
        label, tip = poller.game_label(gid)
        teams = {pl.get("name"): pl.get("team_id") for pl in game.get("players", [])}
        for c in poller.engine_candidates(gid, qs, now, game, design):
            if not qualifies(c, min(BARS.values()), cal):
                continue
            # rules v2 (g): assist props and game markets only, with rules (b) and (c)
            tm = None
            if c.player and c.market == "assists" and c.side == "under":
                with main.connect() as m:
                    tm = EL.teammates_out(m, gid, teams.get(c.player), c.player, now)
            if EL.reason("nextgen", c.market, c.side, c.line, tm) is not None:
                continue
            legs.append(leg_of(c, gid, label, tip, _who(c, game, label), cal))
    return legs


def key(day: date) -> str:
    return f"parlay:nba:{day.isoformat()}"


def select(pdb, poller, main, now: datetime) -> dict:
    """Choose today's parlay once (the morning update's step); stored in the poller state."""
    from betdata.alerts.db import get_state, set_state
    from betdata.alerts.rules import EASTERN

    day = now.astimezone(EASTERN).date()
    with pdb.connect() as p:
        cur = get_state(p, key(day))
    if cur is not None:
        return cur
    pick = choose(candidate_legs(poller, main, now))
    doc = {"day": day.isoformat(), "chosen_at": now.isoformat(), "signal": SIGNAL,
           "stake": STAKE}  # fmt: skip
    if pick is None:
        doc.update(none=True, note=NONE_TEXT)
    else:
        doc.update(kind=pick.kind, book=pick.book, decimal=round(pick.decimal, 4),
                   american=american(pick.decimal), ev=round(pick.ev, 4),
                   p_hit=round(pick.p_hit, 4), legs=[asdict(x) for x in pick.legs])  # fmt: skip
    with pdb.begin() as p:
        set_state(p, key(day), doc, now)
    return doc


def odds_text(doc: dict) -> str:
    a = doc["american"]
    return f"+{a}" if a > 0 else str(a)


def hits_text(p: float) -> str:
    """'hits about 1 in 3'."""
    return f"hits about 1 in {max(1, round(1 / p))}" if p > 0 else "hits about never"


def push_lines(doc: dict | None) -> list[str]:
    """The morning push's Parlay section."""
    from betdata.alerts.notify import BOOKS as NAMES

    if not doc:
        return []
    if doc.get("none"):
        return [f"Parlay: {NONE_TEXT}"]
    out = [f"Parlay ({doc['kind']} legs, {NAMES.get(doc['book'], doc['book'])}): "
           f"{odds_text(doc)}, {doc['stake']:g}u · {hits_text(doc['p_hit'])}"]
    for x in doc["legs"]:
        out.append(f"  {leg_text(x)}")
    return out


def leg_text(x: dict) -> str:
    from types import SimpleNamespace

    from betdata.alerts.notify import _fmt_price, selection
    from betdata.alerts.rules import EASTERN

    a = SimpleNamespace(game_label=x["game"], market=x["market"], side=x["side"],
                        line=x["line"], player_name=x["player"])  # fmt: skip
    when = datetime.fromisoformat(x["tip"]).astimezone(EASTERN).strftime("%-I:%M %p ET")
    eng = x.get("p_raw") if x.get("p_raw") is not None else x["p"]
    cal = (f" (calibrated {100 * x['p_cal']:.0f}%)" if x.get("p_cal") is not None else "")
    return (f"{x['game']} {when}: {selection(a)} {_fmt_price(x['price'])} · Engine "
            f"{100 * eng:.0f}%{cal} · Market {100 * x['p_market']:.0f}%")


# --- grading ---------------------------------------------------------------------------------


def leg_result(m, x: dict) -> str | None:
    """win / loss / push / void, or None while the game or box score is not in."""
    from types import SimpleNamespace

    from betdata.alerts.grade import VOID, prop_actual, prop_result, result_of
    from betdata.alerts.tracker import _final_score

    if x["player"]:
        a = SimpleNamespace(sport="nba", game_id=x["game_id"], player_name=x["player"],
                            market=x["market"])  # fmt: skip
        act = prop_actual(m, a, None)
        if act is None:
            return None
        return "void" if act == VOID else prop_result(x["side"], x["line"], float(act))
    sc = _final_score(m, x["game_id"])
    if sc is None:
        return None
    return result_of(x["market"], x["side"], x["line"], float(sc[0]), float(sc[1]))


def settle(results: list[str], decs: list[float]) -> tuple[str, float, int, float]:
    """(result, profit per unit, legs left, decimal paid) from the legs' results: any loss
    loses; pushes and voids drop out and the rest is re-priced; one leg left grades as that
    leg; none left is void."""
    if "loss" in results:
        return "loss", -1.0, sum(r == "win" for r in results), 0.0
    wins = [d for r, d in zip(results, decs, strict=True) if r == "win"]
    if not wins:
        return "void", 0.0, 0, 0.0
    paid = math.prod(wins)
    return "win", paid - 1, len(wins), paid


def grade(m, doc: dict) -> dict | None:
    """The parlay's result once every leg is decided, else None."""
    res = [leg_result(m, x) for x in doc["legs"]]
    if any(r is None for r in res):
        return None
    result, profit, left, paid = settle(res, [decimal(x["price"]) for x in doc["legs"]])
    return {"result": result, "profit": round(profit, 4), "units": round(STAKE * profit, 4),
            "legs_left": left, "decimal_paid": round(paid, 4), "leg_results": res}


# --- the ledger and the record ----------------------------------------------------------------


def parlay_id(day: str) -> str:
    return f"{SIGNAL}:{day}"


def sync(p, m, out: Path, now: datetime) -> int:
    """Write each chosen parlay once, then each result once (append-only)."""
    from sqlalchemy import select as sql_select

    from betdata.alerts.db import state
    from betdata.ledger import _tables, append_rows, read_rows

    if "poller_state" not in _tables(p):
        return 0
    rows = []
    docs = [st.value for st in p.execute(sql_select(state).where(
        state.c.key.like("parlay:nba:%")))]  # fmt: skip
    for d in docs:
        rows.append({"parlay_id": parlay_id(d["day"]), "day": d["day"], "signal": SIGNAL,
                     "chosen_at": d["chosen_at"], "kind": d.get("kind", ""),
                     "book": d.get("book", ""), "decimal": d.get("decimal", ""),
                     "american": d.get("american", ""), "ev": d.get("ev", ""),
                     "p_hit": d.get("p_hit", ""), "stake": d["stake"],
                     "legs": json.dumps(d.get("legs", []), separators=(",", ":")),
                     "note": d.get("note", "")})  # fmt: skip
    n = append_rows(out / FILE, FIELDS, rows, key="parlay_id")
    if m is None:
        return n
    done = {r["parlay_id"] for r in read_rows(out / RESULTS)}
    res = []
    for d in docs:
        pid = parlay_id(d["day"])
        if pid in done or d.get("none"):
            continue
        g = grade(m, d)
        if g is not None:
            res.append({"parlay_id": pid, "day": d["day"], **g,
                        "leg_results": " ".join(g["leg_results"]),
                        "graded_at": now.isoformat()})  # fmt: skip
    append_rows(out / RESULTS, RESULT_FIELDS, res, key="parlay_id")
    return n


@dataclass
class Record:
    wins: int = 0
    losses: int = 0
    voids: int = 0
    units: float = 0.0
    staked: float = 0.0

    @property
    def graded(self) -> int:
        return self.wins + self.losses

    @property
    def roi(self) -> float | None:
        return self.units / self.staked if self.staked else None

    def text(self) -> str:
        roi = "–" if self.roi is None else f"{100 * self.roi:+.1f}%"
        return (f"Parlay record: {self.wins}-{self.losses} · {self.units:+.2f}u · ROI {roi} · "
                f"{self.graded} graded")


def record(out: Path) -> Record:
    """The parlay's own record (never mixed into either card's): W-L, units, ROI, graded."""
    from betdata.ledger import read_rows

    r = Record()
    for x in read_rows(out / RESULTS):
        if x["result"] == "win":
            r.wins += 1
        elif x["result"] == "loss":
            r.losses += 1
        else:
            r.voids += 1
            continue
        r.units += float(x["units"])
        r.staked += STAKE
    return r


def page_doc(p, m, day: date, out: Path) -> dict | None:
    """The parlay for Today's tracker (Tonight's card, Next Gen side): legs with display text,
    status per leg once decided, the result when graded, and the record line."""
    from betdata.alerts.db import get_state

    doc = get_state(p, key(day))
    if doc is None:
        return None
    rec = record(out)
    view = {"note": doc.get("note"), "none": bool(doc.get("none")), "record": rec.text()}
    if doc.get("none"):
        return view
    from betdata.alerts.notify import BOOKS as NAMES

    legs = []
    for x in doc["legs"]:
        r = None
        if m is not None:
            try:
                r = leg_result(m, x)
            except Exception:  # noqa: BLE001 - a status never drops the parlay
                r = None
        legs.append({**x, "text": leg_text(x), "result": r})
    g = None
    if m is not None and all(x["result"] for x in legs):
        g = grade(m, doc)
    view.update(kind=doc["kind"], book=NAMES.get(doc["book"], doc["book"]),
                odds=odds_text(doc), hits=hits_text(doc["p_hit"]), stake=doc["stake"],
                legs=legs, ev=doc["ev"],
                result=None if g is None else g["result"],
                units=None if g is None else g["units"])  # fmt: skip
    return view
