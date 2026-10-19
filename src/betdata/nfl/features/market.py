"""Group 8: the market at the prediction point. The latest polled spread and total fetched by
then (never a closing line) and the move since the first line we stored. History has no such
lines (BACKLOG.md), so in fitting these are all zero; the market enters live predictions
through the model+market blend (nfl/predict.py)."""

from __future__ import annotations

from betdata.nfl.store import AsOf


def market_at(a: AsOf, game_id: int) -> dict[str, float | None]:
    o = a.df("odds")
    rows = o[o.game_id == game_id]
    out: dict[str, float | None] = {}
    for market in ("spreads", "totals"):
        m = rows[rows.market == market]
        out[market] = float(m.line.iloc[-1]) if len(m) else None
        out[f"{market}_open"] = float(m.line.iloc[0]) if len(m) else None
    return out


class Market:
    name = "mkt"
    version = 1

    def compute(self, a: AsOf, g, point: str) -> dict[str, float]:
        m = market_at(a, g.game_id)
        have = m["spreads"] is not None
        have_t = m["totals"] is not None
        return {
            "m_mkt_line": -m["spreads"] if have else 0.0,
            "m_mkt_move": (m["spreads_open"] - m["spreads"]) if have else 0.0,
            "m_mkt_have": float(have),
            "t_mkt_total": m["totals"] if have_t else 0.0,
            "t_mkt_move": (m["totals"] - m["totals_open"]) if have_t else 0.0,
            "t_mkt_have": float(have_t),
        }
