"""M15 nflverse files: download, skip rules, 404 for an unpublished season, atomic write."""

import dataclasses
import io
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import func, select

from betdata.db.schema import data_files, raw_responses
from betdata.sources.nflverse_extra import DATASETS, NflverseExtraSource


def parquet(**cols) -> bytes:
    out = io.BytesIO()
    pq.write_table(pa.table(cols), out)
    return out.getvalue()


PARTICIPATION = parquet(
    nflverse_game_id=["2024_01_TEN_CHI"], play_id=[63.0], possession_team=["TEN"],
    offense_players=["00-1;00-2"], defense_players=["00-3"], offense_names=["A;B"],
    players_on_play=["00-1;00-2;00-3"], was_pressure=[True],
)  # fmt: skip
FTN = parquet(nflverse_game_id=["2026_07_A_B"], nflverse_play_id=[1], season=[2026], week=[7])


@pytest.fixture
def src(engine, settings, fake_api, clock, tmp_path):
    cfg = settings.source("nflverse_extra")
    cfg = dataclasses.replace(cfg, options={**cfg.options, "data_dir": str(tmp_path / "data")})
    return NflverseExtraSource(engine, cfg, transport=fake_api.transport, sleep=lambda s: None,
                               clock=clock)  # fmt: skip


def versions(engine):
    with engine.connect() as conn:
        return conn.execute(select(data_files).order_by(data_files.c.file_id)).mappings().all()


def test_participation_saved_trimmed_and_versioned(src, engine, fake_api, tmp_path):
    fake_api.add(body=PARTICIPATION)
    result = src.run("participation", seasons="2024")
    assert result.status == "ok", result.message
    rel = "pbp_participation/pbp_participation_2024.parquet"
    assert fake_api.requests[0].url.path.endswith("/" + rel)
    target = tmp_path / "data" / "nflverse" / rel
    cols = pq.read_table(target).column_names
    assert "offense_players" in cols and "was_pressure" in cols
    assert "offense_names" not in cols and "players_on_play" not in cols
    (row,) = versions(engine)
    assert (row["source"], row["dataset"], row["season"]) == ("nflverse_extra", "participation",
                                                             2024)  # fmt: skip
    assert row["path"] == "nflverse/" + rel
    assert row["bytes"] == target.stat().st_size
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(raw_responses)).scalar() == 0
    assert not list(target.parent.glob("*.tmp"))


def test_unpublished_season_is_skipped_not_failed(src, engine, fake_api, tmp_path):
    fake_api.add(status=404, body=b"Not Found")
    result = src.run("participation", seasons="2026")
    assert result.status == "skipped" and "not published" in result.message
    assert len(fake_api.requests) == 1  # a 404 is not retried
    assert not (tmp_path / "data" / "nflverse").exists() and versions(engine) == []


def test_season_before_coverage_makes_no_request(src, fake_api):
    result = src.run("ftn", seasons="2021")
    assert result.status == "skipped" and "starts 2022" in result.message
    assert fake_api.requests == []


def test_past_season_on_disk_is_not_downloaded_again(src, fake_api, clock):
    fake_api.add(body=PARTICIPATION)
    assert src.run("participation", seasons="2024").status == "ok"
    clock.advance(days=30)
    result = src.run("participation", seasons="2024")
    assert result.status == "skipped" and "already on disk" in result.message
    assert len(fake_api.requests) == 1
    fake_api.add(body=PARTICIPATION)
    assert src.run("participation", seasons="2024", refetch="1").status == "ok"


def test_current_season_refreshed_at_most_daily(src, engine, fake_api, clock, tmp_path):
    fake_api.add(body=FTN)
    assert src.run("ftn").status == "ok"  # default: current season (2026)
    clock.advance(hours=6)
    assert src.run("ftn").status == "skipped"
    clock.advance(hours=18)
    fake_api.add(body=FTN)
    assert src.run("ftn").rows_inserted == 0  # unchanged: no new version
    clock.advance(days=1)
    newer = parquet(nflverse_game_id=["2026_07_A_B", "2026_08_A_C"], nflverse_play_id=[1, 2],
                    season=[2026, 2026], week=[7, 8])  # fmt: skip
    fake_api.add(body=newer)
    assert src.run("ftn").rows_inserted == 1
    assert len(fake_api.requests) == 3 and len(versions(engine)) == 2
    assert (tmp_path / "data" / "nflverse" / "ftn_charting" / "ftn_charting_2026.parquet"
            ).read_bytes() == newer  # fmt: skip


def test_missing_file_is_downloaded_again(src, engine, fake_api, tmp_path):
    """A lost data directory (e.g. the Actions runner) is refilled even if data_files says
    the version is current."""
    fake_api.add(body=FTN)
    assert src.run("ftn", seasons="2025").status == "ok"
    target = tmp_path / "data" / "nflverse" / "ftn_charting" / "ftn_charting_2025.parquet"
    target.unlink()
    fake_api.add(body=FTN)
    assert src.run("ftn", seasons="2025").rows_inserted == 1
    assert target.exists()


def test_bad_downloads_fail_and_write_nothing(src, fake_api, tmp_path):
    fake_api.add(body=b"<html>rate limited</html>")
    assert "not a parquet file" in src.run("ftn", seasons="2025").message
    fake_api.add(body=parquet(season=[2025]))
    result = src.run("ftn", seasons="2025")
    assert result.status == "failed" and "missing columns" in result.message
    assert not (tmp_path / "data" / "nflverse").exists()


def test_write_is_atomic(src, fake_api, tmp_path, monkeypatch):
    """If the rename fails the previous file is untouched (readers never see a partial one)."""
    fake_api.add(body=FTN)
    assert src.run("ftn", seasons="2026").status == "ok"
    target = tmp_path / "data" / "nflverse" / "ftn_charting" / "ftn_charting_2026.parquet"

    def boom(a, b):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    fake_api.add(body=parquet(nflverse_game_id=["x"], nflverse_play_id=[2], season=[2026],
                              week=[8]))  # fmt: skip
    with pytest.raises(OSError):
        src.run("ftn", seasons="2026", refetch="1")
    assert target.read_bytes() == FTN


def test_ngs_is_one_file_for_all_seasons(src, engine, fake_api, tmp_path):
    fake_api.add(body=parquet(season=[2016], week=[1], team_abbr=["KC"], player_gsis_id=["00-1"]))
    assert src.run("ngs_passing").status == "ok"
    assert fake_api.requests[0].url.path.endswith("/nextgen_stats/ngs_passing.parquet")
    assert versions(engine)[0]["season"] == 0
    assert (tmp_path / "data" / "nflverse" / "nextgen_stats" / "ngs_passing.parquet").exists()


def test_refresh_runs_every_dataset_for_the_current_season(src, fake_api):
    for _ in DATASETS:
        fake_api.add(status=404)  # nothing out yet (e.g. just after the season rolls over)
    result = src.run("refresh", seasons="2020")  # seasons is ignored: always current
    assert result.status == "ok"
    paths = [r.url.path.split("/download/")[-1] for r in fake_api.requests]
    assert len(paths) == len(DATASETS)
    assert paths[0] == "pbp_participation/pbp_participation_2026.parquet"
    assert all("2020" not in p for p in paths)


def test_unpublished_files_do_not_trip_the_breaker(src, engine, fake_api):
    """Right after a season rolls over every file 404s for months; that is not an outage."""
    from betdata.db.schema import source_health

    for _ in range(8):
        fake_api.add(status=404)
    for _ in range(8):
        assert src.run("ftn", seasons="2026").status == "skipped"
    with engine.connect() as conn:
        health = conn.execute(select(source_health)).mappings().all()
    assert all(h["consecutive_failures"] == 0 and h["open_until"] is None for h in health)
