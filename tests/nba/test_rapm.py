"""M15 lineup RAPM: the ridge recovers known player effects from synthetic stints, snapshots
use only stints known before them, and the group reads the latest snapshot at or before the
prediction point (verification b)."""

import numpy as np
import pandas as pd
import pytest

from betdata.nba.asof import check
from betdata.nba.data import load_store
from betdata.nba.rapm import Rapm, fit_snapshot, mondays, snapshots, value
from tests.nba.synth import synth


def _stints(n=6000, seed=2):
    rnd = np.random.default_rng(seed)
    players = [f"p{i}" for i in range(30)]
    true = {p: (6.0 if p == "p0" else -6.0 if p == "p1" else 0.0) for p in players}
    rows = []
    t0 = pd.Timestamp("2022-11-01", tz="UTC")
    for i in range(n):
        ten = rnd.choice(players, 10, replace=False)
        h, a = sorted(ten[:5]), sorted(ten[5:])
        poss = 10.0
        margin = sum(true[p] for p in h) - sum(true[p] for p in a)
        pts = rnd.normal(margin * poss / 100, 2.0)
        rows.append(dict(game_id=i // 30, start_sec=0, end_sec=120, home_players=",".join(h),
                         away_players=",".join(a), home_pts=max(pts, 0), away_pts=max(-pts, 0),
                         home_poss=poss, away_poss=poss, season=2022,
                         known_at=t0 + pd.Timedelta(hours=i)))  # fmt: skip
    df = pd.DataFrame(rows)
    df["poss"] = 10.0
    df["minutes"] = 2.0
    return df


def test_fit_recovers_effects():
    snap = fit_snapshot(_stints(), 2022, lam=200.0)
    assert snap["p0"][0] > 3 and snap["p1"][0] < -3
    assert abs(np.median([v[0] for p, v in snap.items() if p not in ("p0", "p1")])) < 1


def test_snapshots_only_use_earlier_stints():
    st = _stints()
    t = st.known_at.iloc[3000]
    a = snapshots(st, [t])[t]
    later = st.copy()
    later.loc[later.known_at >= t, "home_pts"] = 99  # results after the snapshot change
    assert snapshots(later, [t])[t] == a


def test_value_blends_box_prior():
    snap = {"x": (4.0, 0.0), "y": (4.0, 10000.0)}
    assert value(snap, "x", 5.0) == pytest.approx(0.6 * 5.0)
    assert value(snap, "y", 5.0) == pytest.approx(4.0, abs=0.2)
    assert mondays(pd.Timestamp("2022-11-02", tz="UTC"),
                   pd.Timestamp("2022-11-20", tz="UTC"))[0].weekday() == 0  # fmt: skip


def test_asof_harness_rapm(engine):
    ids = synth(engine)
    with engine.connect() as conn:
        store = load_store(conn)
    tips = store.games.tip
    snaps = {t: {"00": (5.0, 500.0), "10": (-3.0, 400.0)}
             for t in (tips.min() + pd.Timedelta(days=10), tips.min() + pd.Timedelta(days=20))}
    store = store.with_params(rapm=snaps)
    assert check(store, [Rapm()], ids[5:30:4]) == []
