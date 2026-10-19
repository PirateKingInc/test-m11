"""Shared steps of the M12 workflow: cached feature rows, training-season parameters, the
games a model is fitted on, and closing lines for scoring (never as inputs)."""

from __future__ import annotations


def data_dir():
    import os
    from pathlib import Path

    from betdata.config import PROJECT_ROOT

    return Path(os.environ.get("BETDATA_DATA_DIR") or PROJECT_ROOT / "data")


def feature_rows(store, group, game_ids, refresh: bool = False):
    """One group's features for these games at both points, cached under data/derived."""
    import hashlib

    import pandas as pd

    from betdata.nfl.pipeline import build

    tag = hashlib.sha1(",".join(map(str, sorted(game_ids))).encode()).hexdigest()[:10]
    version = getattr(group, "version", 1)
    path = data_dir() / "derived" / f"features_{group.name}_{version}_{tag}.parquet"
    if path.exists() and not refresh:
        return pd.read_parquet(path)
    rows = build(store, [group], game_ids)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows.to_parquet(path)
    return rows


def closing_lines(conn):
    """game_id -> (home spread, total) at the close. For scoring only, never a feature."""
    from betdata.analysis.dataset import load_games

    return {g.game_id: (g.spread_home, g.total) for g in load_games(conn, "nfl")}


def close_mae(close, out, game_ids):
    import numpy as np

    m = [abs(-close[i][0] - out.at[i, "margin"]) for i in game_ids
         if i in out.index and close.get(i, (None,))[0] is not None]  # fmt: skip
    t = [abs(close[i][1] - out.at[i, "total"]) for i in game_ids
         if i in out.index and close.get(i, (None, None))[1] is not None]  # fmt: skip
    return float(np.mean(m)), float(np.mean(t))


def fitted_params(store, seasons=None):
    """Store params learned on training seasons only (play probabilities, replacement QB),
    cached under data/derived."""
    import json

    from betdata.nfl.features.availability import learn_play_prob
    from betdata.nfl.pipeline import TRAIN

    seasons = tuple(seasons or TRAIN)
    path = data_dir() / "derived" / f"params_{min(seasons)}_{max(seasons)}.json"
    if path.exists():
        params = json.loads(path.read_text())
    else:
        params = learn_play_prob(store, seasons)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(params, indent=1, sort_keys=True))
    return store.with_params(**params)


def model_games(store, seasons):
    g = store.games
    return [i for i in g.index if g.at[i, "season"] in set(seasons)
            and g.at[i, "season_type"] in ("REG", "POST")]  # fmt: skip


def design_rows(store, design, seasons, refresh=False):
    from betdata.nfl.groups import GROUPS as ALL

    ids = model_games(store, seasons)
    rows = None
    for name in design.groups:
        r = feature_rows(store, ALL[name], ids, refresh)
        rows = r if rows is None else rows.merge(
            r.drop(columns=["season", "week"]), on=["game_id", "point"])  # fmt: skip
    return rows
