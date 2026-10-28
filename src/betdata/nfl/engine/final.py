"""M17 Part E: the engine's one scoring on the test seasons (MODEL.md "M17").

The validated design (rate parameters chosen on 2020-2022) is refitted on 2016-2022 (drive
model; calibration on 2021-2022) and run once over 2023-2025 at both points with 10,000
simulations: side outputs, player outputs with P(over) at the ESPN prop lines of 2024-2025,
and the chain-step decomposition at prekick. A second run refuses unless `again` (labelled a
repeat look for NFL sides: M12, M13 and M15 each scored these seasons once).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pandas as pd

from betdata.config import PROJECT_ROOT

log = logging.getLogger(__name__)
REPORT = PROJECT_ROOT / "reports" / "nfl" / "m17_final_test.json"
TEST = (2023, 2024, 2025)
FIT = tuple(range(2016, 2023))


def out_dir() -> Path:
    from betdata.nfl.engine.plays import cache_dir

    return cache_dir() / "final_test"


def run(rates: dict, again: bool = False, sims: int = 10_000, decomp_sims: int = 4_000) -> dict:
    from betdata.nfl.engine import backtest as B
    from betdata.nfl.engine import lines as L
    from betdata.nfl.engine import sim as SM
    from betdata.nfl.engine.rates import RateParams

    done = out_dir() / "games.pkl"
    if done.exists() and not again:
        raise RuntimeError(f"test seasons already scored ({done}); scored once (--again)")
    t0 = time.time()
    cfg = B.EngineConfig(rates=RateParams(**rates), sim=SM.SimParams(n=sims), fit_seasons=FIT)
    eng = B.Engine(cfg)
    cal = B.calibrate(eng, seasons=(2021, 2022), rounds=4)
    pl = L.props(eng.store, eng.ids, (2024, 2025))
    lines = {}
    for r in pl.itertuples(index=False):
        ls = [x for x in (getattr(r, "open_line", None), getattr(r, "close_line", None))
              if x == x and x is not None]  # fmt: skip
        if ls:
            lines.setdefault((r.game_id, r.gsis_id), {})[r.market] = ls
    games, players = [], []
    for season in TEST:
        g, p = eng.run(eng.slots([season]), players=True, prop_lines=lines)
        games.append(g)
        players.append(p)
        log.info("final test %s done (%.0fs)", season, time.time() - t0)
    g, p = pd.concat(games, ignore_index=True), pd.concat(players, ignore_index=True)
    dec = B.decompose_run(eng, eng.slots(TEST, ("prekick",)), n=decomp_sims)
    out = out_dir()
    out.mkdir(parents=True, exist_ok=True)
    g.to_pickle(out / "games.pkl")
    p.to_pickle(out / "players.pkl")
    dec.to_pickle(out / "decomposition.pkl")
    eng.model.save(out / "drive_model.json")
    meta = {"fit_seasons": list(FIT), "test": list(TEST), "rates": rates, "sims": sims,
            "calibration": cal, "minutes": round((time.time() - t0) / 60, 1),
            "label": "repeat look (fourth for NFL sides)"}  # fmt: skip
    (out / "meta.json").write_text(json.dumps(meta, indent=1, default=float))
    return meta
