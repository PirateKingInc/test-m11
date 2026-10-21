"""m15's compact live inputs under data/derived: built from the files once, read back when the
files are gone (the hourly runner keeps only data/derived), rebuilt when a file is newer."""

import os
import shutil

import numpy as np
import pandas as pd
import pytest

from betdata.nfl.live_inputs import sizes
from betdata.nfl.m15cache import cached_tables, changed_only, season_tables
from betdata.nfl.m15data import Expected, Logistic
from tests.conftest import FIXTURES

EXP = Expected(Logistic(6.0, -0.1), Logistic(0.9, -0.8), Logistic(1.4, -0.08), 1.1, 1.2)


@pytest.fixture
def data_dir(tmp_path):
    p = pd.read_parquet(FIXTURES / "nflverse" / "pbp_2025_sample.parquet")
    rng = np.random.default_rng(0)
    p = p.assign(play_id=np.arange(len(p)), qb_hit=0.0, kick_distance=np.where(
        p.play_type == "field_goal", 40.0, np.nan), field_goal_result=np.where(
        p.play_type == "field_goal", "made", None), kicker_player_id="K1",
        fixed_drive=p.drive, fixed_drive_result=rng.choice(["Touchdown", "Punt"], len(p)),
        third_down_converted=(rng.random(len(p)) < 0.4).astype(float))  # fmt: skip
    (tmp_path / "pbp" / "nfl").mkdir(parents=True)
    p.to_parquet(tmp_path / "pbp" / "nfl" / "play_by_play_2025.parquet")
    return tmp_path


def test_tables_are_cached_and_read_back_without_the_files(data_dir):
    tg, k = cached_tables(data_dir, [2025], EXP)
    direct, _ = season_tables(data_dir, 2025, EXP)
    pd.testing.assert_frame_equal(tg, direct)
    assert len(tg) == 4 and tg.db.sum() > 0  # 2 games x 2 teams
    assert {p.name.split("_2025")[0] for p in (data_dir / "derived").iterdir()} == {
        "nfl_m15_tg", "nfl_m15_kick"}
    shutil.rmtree(data_dir / "pbp")  # the next hourly run: no play-by-play on disk
    tg2, k2 = cached_tables(data_dir, [2024, 2025], EXP)
    pd.testing.assert_frame_equal(tg2, tg)
    assert len(k2) == len(k)
    assert set(sizes(data_dir)) == {p.name for p in (data_dir / "derived").iterdir()}


def test_a_newer_file_rebuilds_and_nothing_at_all_refuses(data_dir):
    cached_tables(data_dir, [2025], EXP)
    cache = next((data_dir / "derived").glob("nfl_m15_tg_*"))
    old = cache.stat().st_mtime - 100
    os.utime(cache, (old, old))
    cached_tables(data_dir, [2025], EXP)
    assert cache.stat().st_mtime > old
    with pytest.raises(ValueError, match="no play-by-play"):
        cached_tables(data_dir, [2019], EXP)


def test_changed_only_keeps_snapshots_that_differ():
    def snap(day, starter):
        return pd.DataFrame({"team": "KC", "as_of": pd.Timestamp(f"2026-10-{day:02d}", tz="UTC"),
                             "position": "QB", "rank": [1, 2], "gsis_id": [starter, "B"]})

    charts = pd.concat([snap(1, "A"), snap(2, "A"), snap(3, "C"), snap(4, "C"), snap(5, "A")])
    kept = changed_only(charts)
    assert sorted(kept.as_of.dt.day.unique()) == [1, 3, 5]
