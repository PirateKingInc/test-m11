# Project decisions log

Decisions that change dependencies, data rules or scope, newest last.

| Date | Decision | Reason |
|---|---|---|
| 2026-10-19 | M12 starts: NFL model with as-of data, two prediction points per game (`final_report`, `prekick`). | See MODEL.md. |
| 2026-10-19 | Injury `date_modified` is stored in `injuries.report_date`; existing rows are filled from archived raw files. | It is the only publish timestamp nflverse gives; needed for as-of injury data (REVIEW.md #3). |
| 2026-10-19 | Add numpy, pandas, pyarrow. | Group 1/2 features need play-by-play (parquet, ~45k plays a season) aggregated per team and player; pandas/pyarrow read it, numpy does the ridge fits and rating solves. |
| 2026-10-19 | Derived tables `nfl_game_info`, `nfl_team_game_pbp`, `nfl_player_game_pbp` (migration 0010), rebuilt by `betdata nfl derive` from stored files only. | Feature inputs with an explicit `known_at`; rebuilding needs no network. |
| 2026-10-19 | Add scikit-learn. | Only for the gradient-boosting challenger in `betdata nfl select`; ridge won on validation, so it isn't used for predictions. |
| 2026-10-19 | Final design frozen in `config/nfl_model.json`; test seasons 2023-2025 scored once (`betdata nfl final-test` refuses a second run). | MODEL.md "Final test". |
