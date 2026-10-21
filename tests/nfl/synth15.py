"""The synthetic league of synth.py plus the M15 frames (m15_tg, m15_kick, m15_depth)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from betdata.nfl.derive import RESULT_DELAY
from betdata.nfl.points import prekick_time
from betdata.nfl.store import Frame, Store
from tests.nfl.synth import POS, make_store

DEPTH = {"QB": "QB", "OL": "OL", "WR": "WR", "TE": "TE", "RB": "RB", "DL": "DL", "LB": "LB",
         "CB": "DB", "S": "DB"}  # fmt: skip


def make_store15(seed: int = 2) -> Store:
    store = make_store()
    rng = np.random.default_rng(seed)
    tg, kick, depth = [], [], []
    for r in store.games.itertuples(index=False):
        known = r.kickoff + RESULT_DELAY
        for team, opp in ((r.home, r.away), (r.away, r.home)):
            db, car = float(rng.integers(30, 45)), float(rng.integers(18, 32))
            tg.append(dict(game_id=r.game_id, season=r.season, team=team, opp=opp,
                           kickoff=r.kickoff, known_at=known, db=db,
                           sacks=float(rng.integers(0, 6)),
                           press=float(rng.integers(5, 15)), press_n=db,
                           ttt_sum=db * rng.normal(2.8, 0.2), ttt_n=db, car=car,
                           rush_epa=rng.normal(-0.05, 0.1) * car, rush_succ=car * 0.4,
                           punt_n=4.0, punt_epa=rng.normal(0, 2), ko_n=5.0, ko_epa=rng.normal(0, 2),
                           ret_n=8.0, ret_epa=rng.normal(0, 2), td3_n=13.0,
                           td3_conv=float(rng.integers(3, 9)), td3_exp=5.3, rz_n=4.0,
                           rz_td=float(rng.integers(0, 5)), rz_exp=2.2, off_plays=65.0,
                           off_pen=float(rng.integers(1, 7)), off_pen_yds=40.0, def_plays=65.0,
                           def_pen=float(rng.integers(1, 7)), def_pen_yds=35.0))  # fmt: skip
            n = float(rng.integers(0, 4))
            kick.append(dict(game_id=r.game_id, season=r.season, team=team, opp=opp,
                             kicker=f"K{team}-{int(rng.random() < 0.1)}", fg_n=n,
                             fg_made=float(rng.binomial(int(n), 0.8)), fg_exp=0.85 * n,
                             kickoff=r.kickoff, known_at=known))  # fmt: skip
            for grp, n_slots in POS:
                order = list(rng.permutation(n_slots + 1))
                for rank, j in enumerate(order):
                    slot = f"{grp}{min(rank, n_slots - 1)}"
                    depth.append(dict(season=r.season, team=team, position=grp,
                                      group=DEPTH[grp], rank=1 + rank // n_slots, slot=slot,
                                      gsis_id=f"P{team}-{grp}{j}",
                                      known_at=prekick_time(r.kickoff)))  # fmt: skip
    frames = {**store.frames, "m15_tg": Frame(pd.DataFrame(tg)),
              "m15_kick": Frame(pd.DataFrame(kick)), "m15_depth": Frame(pd.DataFrame(depth))}
    return Store(store.games, frames)
