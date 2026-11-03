"""M20: one NBA engine (MODEL.md "M20"). Player rates -> minutes -> team assembly -> 10,000
correlated simulations per game; blended with the market only after it has predicted.

The market never enters a rate, fit or constant: no module in this package reads a line table
(`odds_snapshots`, `prop_lines`, quotes); `tests/nba/engine/test_no_market.py` checks it. The
blend (Part C) and the pick rule (Part E) live in `blend.py` and `picks.py` and take market
probabilities as arguments.
"""

MODEL = "nba-engine"
VERSION = "nbae20"
