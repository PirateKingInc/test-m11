"""M15 nflverse file readers: columns, as-of times, and the two depth-chart formats."""

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from betdata.nfl import nflverse_files as nf

T = pd.Timestamp
KICK = pd.Series({
    "2024_06_LV_DEN": T("2024-10-13T20:05Z"),   # Sunday
    "2024_06_BUF_NYJ": T("2024-10-15T00:15Z"),  # Monday night: the week's last kickoff
    "2024_07_KC_SF": T("2024-10-20T20:25Z"),
    "2016_01_LA_SF": T("2016-09-13T02:20Z"),
    "2016_02_SEA_LA": T("2016-09-18T20:05Z"),
})  # fmt: skip


def write(root: Path, rel: str, **cols) -> None:
    path = root / "nflverse" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(cols), path)


def test_game_files_known_twelve_hours_after_kickoff(tmp_path):
    write(tmp_path, "pbp_participation/pbp_participation_2024.parquet",
          nflverse_game_id=["2024_06_LV_DEN", "2024_99_X_Y"], play_id=[1.0, 2.0],
          offense_players=["00-1;00-2", "00-3"], was_pressure=[True, False])  # fmt: skip
    df = nf.load_participation([2024, 2026], kickoffs=KICK, data_dir=tmp_path)  # 2026: no file
    assert df["known_at"].iloc[0] == T("2024-10-14T08:05Z")
    assert pd.isna(df["known_at"].iloc[1])  # unknown game: never "known"
    assert df["offense_players"].iloc[0].split(";") == ["00-1", "00-2"]
    assert "known_at" not in nf.load_participation([2024], data_dir=tmp_path)

    write(tmp_path, "ftn_charting/ftn_charting_2024.parquet", nflverse_game_id=["2024_07_KC_SF"],
          nflverse_play_id=[5], n_blitzers=[2], is_play_action=[True])  # fmt: skip
    assert nf.load_ftn([2024], kickoffs=KICK, data_dir=tmp_path)["known_at"].tolist() == [
        T("2024-10-21T08:25Z")]  # fmt: skip

    write(tmp_path, "pfr_advstats/advstats_week_def_2024.parquet", game_id=["2024_06_BUF_NYJ"],
          pfr_player_id=["AbcdXx00"], def_pressures=[3.0])  # fmt: skip
    pfr = nf.load_pfr("def", range(2018, 2025), kickoffs=KICK, data_dir=tmp_path)
    assert (pfr["def_pressures"].tolist(), pfr["known_at"].tolist()) == (
        [3.0], [T("2024-10-15T12:15Z")])  # fmt: skip
    with pytest.raises(ValueError):
        nf.load_pfr("kicking", [2024], data_dir=tmp_path)

    write(tmp_path, "stats_player/stats_player_week_2024.parquet", game_id=["2024_07_KC_SF"],
          player_id=["00-9"], targets=[7], receiving_yards=[88])  # fmt: skip
    stats = nf.load_player_stats([2024], kickoffs=KICK, columns=["game_id", "targets"],
                                 data_dir=tmp_path)  # fmt: skip
    assert list(stats.columns) == ["game_id", "targets", "known_at"]


def test_missing_files_give_an_empty_frame(tmp_path):
    assert nf.load_participation([2026], data_dir=tmp_path).empty
    assert nf.load_ngs("passing", kickoffs=KICK, data_dir=tmp_path).empty
    assert list(nf.load_depth_charts([2026], data_dir=tmp_path).columns) == nf.DEPTH_COLUMNS


def test_ngs_rows_placed_by_team_week_or_the_season_end(tmp_path):
    write(tmp_path, "nextgen_stats/ngs_passing.parquet",
          season=[2016, 2016, 2016, 2016], week=[1, 2, 0, 1],
          team_abbr=["LAR", "LAR", "LAR", None], player_gsis_id=["a", "a", "a", "b"],
          avg_time_to_throw=[2.7, 2.8, 2.75, 3.0])  # fmt: skip
    df = nf.load_ngs("passing", kickoffs=KICK, data_dir=tmp_path)
    # NGS says LAR; the game ids of the time say LA. Week 0 (season totals): the last game.
    assert df["known_at"].tolist()[:3] == [T("2016-09-13T14:20Z"), T("2016-09-19T08:05Z"),
                                           T("2016-09-19T08:05Z")]  # fmt: skip
    assert pd.isna(df["known_at"].iloc[3])
    with pytest.raises(ValueError):
        nf.load_ngs("kicking", data_dir=tmp_path)


def weekly_chart(root: Path) -> None:
    write(root, "depth_charts/depth_charts_2024.parquet",
          season=[2024] * 6, club_code=["DEN", "DEN", "DEN", "DEN", "KC", "DEN"],
          week=[6.0, 6.0, 6.0, 6.0, 6.0, None], game_type=["REG"] * 6,
          depth_team=["1", "2", "1", "1", "1", "1"],
          depth_position=["LT", "LT", "\n    ", "NB", "QB", "QB"],
          position=["T", "T", "G", "CB", "QB", "QB"],
          gsis_id=["00-1", "00-2", "00-3", "00-4", "00-5", "00-6"],
          full_name=["A", "B", "C", "D", "Mahomes", "No week"],
          formation=["Offense", "Offense", "Offense", "Defense", "Offense", "Offense"])  # fmt: skip


def daily_chart(root: Path) -> None:
    write(root, "depth_charts/depth_charts_2026.parquet",
          dt=["2026-10-17T12:00:00Z"] * 3 + ["2026-10-18T12:00:00Z"] * 3,
          team=["ARI"] * 6, player_name=["Sweat", "Leota", "Sweat", "Sweat", "Leota", "Kicker"],
          espn_id=["1", "2", "1", "1", "2", "3"],
          gsis_id=["00-7", "00-8", "00-7", "00-7", "00-8", "00-9"],
          pos_grp=["Base 4-3 D"] * 5 + ["Special Teams"],
          pos_abb=["LDE", "LDE", "LDE", "LDE", "RDE", "PK"],
          pos_slot=[1, 1, 1, 1, 4, 1], pos_rank=[1, 2, 1, 1, 1, 1])  # fmt: skip


def test_depth_charts_both_formats_in_one_frame(tmp_path):
    weekly_chart(tmp_path)
    daily_chart(tmp_path)
    df = nf.load_depth_charts([2024, 2025, 2026], kickoffs=KICK, data_dir=tmp_path)
    assert list(df.columns) == nf.DEPTH_COLUMNS
    w = df[df["format"] == "weekly"].set_index("gsis_id")
    assert w.loc["00-1", ["position", "group", "rank", "team", "week"]].tolist() == [
        "LT", "OL", 1, "DEN", 6]  # fmt: skip
    assert w.loc["00-2", "rank"] == 2
    assert w.loc["00-3", "position"] == "OL"  # blank slot: roster position G, side unknown
    assert (w.loc["00-4", "position"], w.loc["00-4", "group"]) == ("CB", "DB")
    assert "00-6" not in w.index  # row without a week
    # Week 6 chart: from DEN's week-6 prekick (kickoff - 75 min); KC had a bye: the week's
    # last kickoff (Monday night).
    assert w.loc["00-1", "as_of"] == T("2024-10-13T18:50Z")
    assert w.loc["00-5", "as_of"] == T("2024-10-15T00:15Z")

    d = df[df["format"] == "daily"]
    assert set(d["season"]) == {2026} and d["week"].isna().all()
    first = d[d["as_of"] == T("2026-10-17T12:00Z")]
    assert len(first) == 2  # Sweat listed twice at LDE that day: kept once
    assert first.set_index("name")["rank"].to_dict() == {"Sweat": 1, "Leota": 2}
    assert set(d["position"]) == {"DE", "K"} and set(d["group"]) == {"DL", "ST"}


def test_depth_chart_at_uses_only_charts_known_by_then(tmp_path):
    weekly_chart(tmp_path)
    daily_chart(tmp_path)
    df = nf.load_depth_charts([2024, 2026], kickoffs=KICK, data_dir=tmp_path)
    assert nf.depth_chart_at(df, T("2024-10-13T18:00Z")).empty  # before DEN's prekick
    den = nf.depth_chart_at(df, T("2024-10-13T19:00Z"))
    assert set(den["team"]) == {"DEN"}
    ari = nf.depth_chart_at(df, T("2026-10-19T00:00Z"))
    ari = ari[ari["team"] == "ARI"]
    assert set(ari["as_of"]) == {T("2026-10-18T12:00Z")}
    assert ari.set_index("name")["position"].to_dict() == {"Sweat": "DE", "Leota": "DE",
                                                           "Kicker": "K"}  # fmt: skip


def test_weekly_charts_without_a_schedule_are_never_known(tmp_path):
    weekly_chart(tmp_path)
    df = nf.load_depth_charts([2024], data_dir=tmp_path)
    assert df["as_of"].isna().all()
    assert nf.depth_chart_at(df, T("2030-01-01T00:00Z")).empty


def test_kickoffs_from_the_stored_schedule(loaded, engine):
    with engine.connect() as conn:
        kick = nf.kickoffs(conn)
    assert kick["2024_22_KC_PHI"] == T("2025-02-09T23:30Z")
    games = nf.team_games(kick)
    sb = games[games["game_id"] == "2024_22_KC_PHI"]
    assert sorted(sb["team"]) == ["KC", "PHI"] and set(sb["week"]) == {22}
