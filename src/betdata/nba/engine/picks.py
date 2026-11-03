"""M20 Part E: one pick rule for the engine (research signal `nba-engine`).

Fair probability = w p_engine + (1 - w) p_market (Part C, w per market from validation), then
the validation Platt fit where one was applied. A pick when EV = fair x decimal odds - 1 at
the best DraftKings/FanDuel price clears theta (game markets / props). One pick per market per
player/game (dedup `engine:<game>:<market>:<player>`). p_market is the M18 consensus at the
book's line when one exists, else the book's own no-vig."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from betdata.nba.engine.evaluate import apply_platt, dec
from betdata.nba.engine.live import p_hist_above, p_over

BOOKS = ("draftkings", "fanduel")
PROP_MARKETS = {"points": "pts", "rebounds": "reb", "assists": "ast", "threes": "fg3m",
                "pra": "pra"}  # fmt: skip


@dataclass
class Design:
    w: dict  # market -> blend weight on the engine
    platt: dict  # market -> (slope, intercept) or None
    theta: dict  # "game" / "prop" -> EV threshold
    validated: dict  # "game" / "prop" -> bool (positive validation ROI at theta)

    @classmethod
    def from_json(cls, d: dict) -> Design:
        return cls(d["w"], {k: (tuple(v) if v else None) for k, v in d["platt"].items()},
                   d["theta"], d.get("validated", {}))  # fmt: skip


@dataclass
class Candidate:
    market: str
    side: str
    line: float | None
    price: int
    book: str
    p_engine: float
    p_market: float
    market_source: str
    fair: float
    ev: float
    player: str | None = None
    book_nv: float | None = None  # the book's own no-vig probability of the side


def no_vig(a: int, b: int) -> float:
    pa, pb = 1 / float(dec(a)), 1 / float(dec(b))
    return pa / (pa + pb)


def fair_prob(design: Design, market: str, p_eng: float, p_mkt: float) -> float:
    w = design.w.get(market, 0.0)
    f = w * p_eng + (1 - w) * p_mkt
    return float(apply_platt(np.array([f]), design.platt.get(market))[0])


def _engine_side(game: dict, market: str, line: float | None) -> float | None:
    """The engine's probability of the home side / the over, given no push."""
    if market == "h2h":
        return float(game["p_home_win"])
    if market == "spreads":
        p, push = p_hist_above(game["margin"], -line)  # home covers: margin + line > 0
    else:
        p, push = p_hist_above(game["total"], line)
    return p / max(1 - push, 1e-9)


def game_candidates(game: dict, two_way: dict, design: Design, cons=None) -> list[Candidate]:
    """two_way: market -> {book: (line, price_a, price_b)} with a = home (h2h, spreads) / over.
    cons(market, side, line) -> consensus probability or None."""
    out = []
    for market in ("spreads", "totals", "h2h"):
        for book, (line, pa, pb) in (two_way.get(market) or {}).items():
            if book not in BOOKS or pa is None or pb is None:
                continue
            pe = _engine_side(game, market, line)
            if pe is None:
                continue
            a_side = "over" if market == "totals" else "home"
            b_side = "under" if market == "totals" else "away"
            pm = cons(market, a_side, line) if cons else None
            src = "consensus" if pm is not None else "book"
            nv = no_vig(pa, pb)
            pm = nv if pm is None else pm
            fa = fair_prob(design, market, pe, pm)
            for side, price, f, p_e, p_m, ln, bn in (
                    (a_side, pa, fa, pe, pm, line, nv),
                    (b_side, pb, 1 - fa, 1 - pe, 1 - pm,
                     None if line is None else (-line if market == "spreads" else line),
                     1 - nv)):
                out.append(Candidate(market, side, ln, int(price), book, p_e, p_m, src, f,
                                     f * float(dec(price)) - 1, None, bn))  # fmt: skip
    return out


def prop_candidates(game: dict, props: dict, design: Design, cons=None,
                    norm=lambda s: s.lower()) -> list[Candidate]:  # fmt: skip
    """props: (normalized player name, market) -> {book: (line, over, under)}."""
    by_name = {norm(p["name"]): p for p in game["players"] if p.get("name")}
    out = []
    for (name, market), books in props.items():
        stat = PROP_MARKETS.get(market)
        pl = by_name.get(name)
        if stat is None or pl is None or not pl["cdf"].get(stat):
            continue
        for book, (line, po, pu) in books.items():
            if book not in BOOKS or po is None or pu is None or line is None:
                continue
            r = p_over(pl["cdf"][stat], line)
            if r is None:
                continue
            pe = r[0] / max(1 - r[1], 1e-9)
            pm = cons(market, pl["name"], line) if cons else None
            src = "consensus" if pm is not None else "book"
            nv = no_vig(po, pu)
            pm = nv if pm is None else pm
            fo = fair_prob(design, market, pe, pm)
            for side, price, f, p_e, p_m, bn in (("over", po, fo, pe, pm, nv),
                                                  ("under", pu, 1 - fo, 1 - pe, 1 - pm,
                                                   1 - nv)):
                out.append(Candidate(market, side, line, int(price), book, p_e, p_m, src, f,
                                     f * float(dec(price)) - 1, pl["name"], bn))  # fmt: skip
    return out


def best_picks(cands: list[Candidate], design: Design) -> list[Candidate]:
    """The best-EV candidate per (market, player) when it clears its theta."""
    best: dict = {}
    for c in cands:
        k = (c.market, c.player)
        if k not in best or c.ev > best[k].ev:
            best[k] = c
    out = []
    for c in best.values():
        th = design.theta["prop" if c.player else "game"]
        if c.ev >= th:
            out.append(c)
    return out


def dedup_key(game_id: int, c: Candidate) -> str:
    return f"engine:{game_id}:{c.market}:{(c.player or '').lower()}"
