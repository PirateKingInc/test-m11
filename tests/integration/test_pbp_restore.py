"""A play-by-play file recorded in `data_files` but missing on disk (a database restored onto a
fresh machine, REVIEW M15-4) is written again; a recorded file already on disk is not."""

from datetime import UTC, datetime

from betdata.sources.nflverse_data import PbpFile, store_pbp

BODY = b"PAR1" + b"x" * 64 + b"PAR1"


def test_rewrites_missing_file(engine, tmp_path):
    f = PbpFile(season=2025, url="u", body=BODY, fetched_at=datetime(2026, 10, 21, tzinfo=UTC))
    target = tmp_path / "pbp" / "nfl" / "play_by_play_2025.parquet"
    with engine.begin() as conn:
        assert store_pbp(conn, f, tmp_path) == (1, 1)
        assert store_pbp(conn, f, tmp_path) == (0, 1) and target.exists()
        target.unlink()
        assert store_pbp(conn, f, tmp_path) == (0, 1)
    assert target.read_bytes() == BODY
