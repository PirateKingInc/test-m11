`games_sample.csv`: real rows copied from nflverse `schedules/games.csv` (downloaded
2026-10-15), header unchanged. Covers a 1999 game (old team codes, no moneyline), a Super Bowl
(neutral, final), 2025 finals including a neutral-site game, upcoming 2026 games with current
lines (one in London), and a 2026 game with no lines yet.

`injuries_2026.csv`: real rows from nflverse `injuries/injuries_2026.csv` (downloaded
2026-10-16). `play_by_play_2025.parquet`: not real play-by-play, just bytes with parquet's
start/end markers; the pipeline only stores and versions these files, it never reads them.

`stats_team_week_2024.csv`, `snap_counts_2024.csv`, `roster_weekly_2024.csv`,
`roster_weekly_2015.csv`: real rows from nflverse `stats_team/`, `snap_counts/` and
`weekly_rosters/` (downloaded 2026-10-18), header unchanged. The 2024 roster rows are the
Eagles in the Super Bowl week, including RES/RET and one practice-squad (DEV) row; the 2015
rows use the older team codes (BLT, CLV, HST, ARZ, SL).
