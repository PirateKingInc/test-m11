"""M9: nflverse injuries (change-only history) and play-by-play files."""

import dataclasses

import pytest
from sqlalchemy import func, select

from betdata.db.schema import data_files, injuries, raw_responses, teams
from betdata.sources.nflverse import NflverseSource
from tests.conftest import fixture_bytes

INJURIES = fixture_bytes("nflverse", "injuries_2026.csv")
PBP = fixture_bytes("nflverse", "play_by_play_2025.parquet")


@pytest.fixture
def nfl(engine, settings, fake_api, clock, tmp_path):
    cfg = settings.source("nflverse")
    cfg = dataclasses.replace(cfg, options={**cfg.options, "data_dir": str(tmp_path / "data")})
    return NflverseSource(engine, cfg, transport=fake_api.transport, sleep=lambda s: None,
                          clock=clock)  # fmt: skip


def injury_rows(engine, name=None):
    with engine.connect() as conn:
        q = select(injuries, teams.c.abbrev).join(teams).order_by(injuries.c.injury_id)
        if name:
            q = q.where(injuries.c.player_name == name)
        return conn.execute(q).mappings().all()


def test_injuries_loaded(nfl, engine, fake_api):
    fake_api.add(body=INJURIES)
    result = nfl.run("injuries")
    assert result.status == "ok", result.message
    assert fake_api.requests[0].url.path.endswith("/injuries/injuries_2026.csv")
    assert (result.rows_inserted, result.rows_seen) == (6, 6)
    out = [r for r in injury_rows(engine, "Andrew Wingard") if r["week"] == 5][0]
    assert (out["report_status"], out["practice_status"], out["detail"]) == (
        "Out", "Did Not Participate In Practice", "Knee")  # fmt: skip
    assert out["practice_detail"] == "Knee" and out["abbrev"] == "ARI"
    assert out["source_player_id"] and out["season"] == 2026


def test_injury_status_changes_are_appended(nfl, engine, fake_api, clock):
    fake_api.add(body=INJURIES)
    nfl.run("injuries")
    clock.advance(days=2)
    # Friday report: Duvernay moves from Limited to Out; everyone else unchanged
    friday = INJURIES.replace(
        b"Devin Duvernay,Devin,Duvernay,,,,Ribs,,Limited Participation in Practice",
        b"Devin Duvernay,Devin,Duvernay,Ribs,,Out,Ribs,,Did Not Participate In Practice",
    )
    assert friday != INJURIES
    fake_api.add(body=friday)
    result = nfl.run("injuries")
    assert result.rows_inserted == 1
    history = injury_rows(engine, "Devin Duvernay")
    assert [(h["report_status"], h["practice_status"]) for h in history] == [
        (None, "Limited Participation in Practice"),
        ("Out", "Did Not Participate In Practice"),
    ]


def test_unchanged_injury_file_adds_nothing(nfl, engine, fake_api, clock):
    fake_api.add(body=INJURIES).add(body=INJURIES)
    nfl.run("injuries")
    clock.advance(hours=6)
    assert nfl.run("injuries").rows_inserted == 0
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(raw_responses)).scalar() == 1


def test_injury_file_shape_change_fails(nfl, fake_api):
    fake_api.add(body=INJURIES.replace(b"report_status", b"game_status"))
    result = nfl.run("injuries")
    assert result.status == "failed" and "report_status" in result.message


def test_pbp_saved_as_file_and_versioned(nfl, engine, fake_api, tmp_path, clock):
    fake_api.add(body=PBP)
    result = nfl.run("pbp", seasons="2025")
    assert result.status == "ok", result.message
    target = tmp_path / "data" / "pbp" / "nfl" / "play_by_play_2025.parquet"
    assert target.read_bytes() == PBP
    with engine.connect() as conn:
        row = conn.execute(select(data_files)).mappings().one()
        raws = conn.execute(select(func.count()).select_from(raw_responses)).scalar()
    assert (row["season"], row["bytes"], row["path"]) == (
        2025,
        len(PBP),
        "pbp/nfl/play_by_play_2025.parquet",
    )
    assert row["url"].endswith("/pbp/play_by_play_2025.parquet")
    assert raws == 0  # big files stay out of the raw archive

    clock.advance(days=1)
    fake_api.add(body=PBP)
    assert nfl.run("pbp", seasons="2025").rows_inserted == 0  # unchanged: no new version
    clock.advance(days=1)
    newer = PBP.replace(b"fake", b"FAKE")
    fake_api.add(body=newer)
    assert nfl.run("pbp", seasons="2025").rows_inserted == 1
    assert target.read_bytes() == newer
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(data_files)).scalar() == 2
    assert not list(target.parent.glob("*.tmp"))


def test_pbp_rejects_non_parquet(nfl, fake_api, tmp_path):
    fake_api.add(body=b"<html>rate limited</html>")
    result = nfl.run("pbp", seasons="2025")
    assert result.status == "failed" and "not a parquet file" in result.message
    assert not (tmp_path / "data" / "pbp").exists()


def test_one_season_per_run(nfl, fake_api):
    result = nfl.run("pbp", seasons="2020-2021")
    assert result.status == "failed" and "one season per run" in result.message
    assert fake_api.requests == []
