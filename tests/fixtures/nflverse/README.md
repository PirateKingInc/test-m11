`games_sample.csv`: real rows copied from nflverse `schedules/games.csv` (downloaded
2026-10-15), header unchanged. Covers a 1999 game (old team codes, no moneyline), a Super Bowl
(neutral, final), 2025 finals including a neutral-site game, upcoming 2026 games with current
lines (one in London), and a 2026 game with no lines yet.

`injuries_2026.csv`: real rows from nflverse `injuries/injuries_2026.csv` (downloaded
2026-10-16). `play_by_play_2025.parquet`: not real play-by-play, just bytes with parquet's
start/end markers; the pipeline only stores and versions these files, it never reads them.
