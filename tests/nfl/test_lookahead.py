"""Step 3c: changing any result after a prediction point never changes that prediction."""

import numpy as np
import pandas as pd

from betdata.nfl.groups import GROUPS
from betdata.nfl.pipeline import build, fit_point, names, outcomes
from betdata.nfl.points import point_time
from betdata.nfl.store import Frame, Store
from tests.nfl.synth import make_store


def predict(store: Store, game_id: int, point: str) -> tuple[float, float]:
    groups = list(GROUPS.values())
    train_ids = [i for i in store.games.index if store.games.at[i, "season"] == 2018]
    rows = build(store, groups, [*train_ids, game_id], points=[point])
    fit = fit_point(rows, outcomes(store), point, [2018], names(rows, "m_"), names(rows, "t_"),
                    10.0, 10.0)  # fmt: skip
    x = rows[rows.game_id == game_id].iloc[0].to_dict()
    return fit.predict(x)


def test_later_results_do_not_change_a_prediction():
    store = make_store()
    g = store.games
    target = int(g[(g.season == 2019) & (g.week == 5)].index[0])
    for point in ("final_report", "prekick"):
        before = predict(store, target, point)
        t = point_time(point, g.at[target, "kickoff"])
        res = store.frame("results").df.copy()
        later = res["known_at"] > pd.Timestamp(t)
        assert later.any() and (~later).any()
        res.loc[later, "home_score"] = res.loc[later, "home_score"] + 30  # rewrite the future
        changed = Store(g, {**store.frames, "results": Frame(res)})
        assert np.allclose(before, predict(changed, target, point))
        # ...while changing an earlier result does move it (the test can see a change).
        res2 = store.frame("results").df.copy()
        earlier = res2.index[~later][-20:]
        res2.loc[earlier, "home_score"] = res2.loc[earlier, "home_score"] + 30
        moved = Store(g, {**store.frames, "results": Frame(res2)})
        assert not np.allclose(before, predict(moved, target, point))
