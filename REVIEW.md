# Code review before M12 (2026-10-19)

Whole codebase read at `fa489d0` (M11). Baselines reproduced on a fresh nflverse load before
any change: Elo 10.18 vs closing line 9.81 on 2015-2025 (990 bets, 50.9%); M11 units+missing
10.09 margin / 10.65 total on 2020-2025. Both match the README exactly.

Severity: **bug** = wrong result, **risk** = could go wrong or mislead, **cleanup** = tidiness.
Every look-ahead leak is a bug and is fixed before any M12 feature is built.

## Findings

| # | Where | Severity | Finding | Action |
|---|---|---|---|---|
| 1 | `src/betdata/analysis/context.py:491-506` | **bug (look-ahead)** | `absences` reads the weekly roster of the game's own week. nflverse's weekly roster is the game-day snapshot: it holds game-day inactives (INA) and any IR/release made after the final injury report (Saturday moves, roster cuts). M11 claims to predict from the final injury report, so a starter placed on IR or cut after Friday counts as known. | **Fix now**: at the final-report point use the previous week's roster (known once that week's games are played); a player listed on the injury report keeps his report status. M11 numbers before and after the fix go in the "Fix results" section below. |
| 2 | `src/betdata/analysis/context.py:424-439` | **bug (look-ahead)** | Injury status = latest row per player-week with no time filter. In a backfill, every row is the file's final version, including rows nflverse modified after the final report or after kickoff (0.38% of 2013-2025 rows were modified within 24 h of kickoff; 0.04% after kickoff). | **Fix now**: keep only rows published before the prediction point (needs #3). |
| 3 | `src/betdata/sources/nflverse_data.py:341-369`, `src/betdata/db/schema.py:256` | bug | The injuries file carries `date_modified` (publish time) but the parser drops it, and `injuries.report_date` is never filled. Without it, injuries have no as-of time. | **Fix now**: parse `date_modified` into `report_date` (additive; change-only tracking unchanged). Fill existing rows from the archived raw files (`betdata nfl backfill-report-dates`, which reads raw_responses and makes no network call). |
| 4 | `src/betdata/cli.py:487-492` | risk | `predict --model units` defaults to alpha 0.05 / revert 0.5 whatever the backtest chose; a stale default silently changes the live model. | Backlog (the M12 predict command stores its own fitted version). |
| 5 | `src/betdata/sources/nflverse_data.py:388` | risk | Injury change-only key is (week, player id, name) without team: a player traded mid-week has two team rows that overwrite each other's "previous" state. Rare. | Backlog. |
| 6 | `src/betdata/analysis/context.py:394-397` | risk | Snap-count players without a PFR->GSIS link are matched by (season, team, normalised name); two players with the same name on one team would merge. | Backlog (M12 player keys go through the same map; logged count of unmatched names). |
| 7 | `src/betdata/analysis/units_backtest.py:95-105` | risk | alpha/revert are chosen by the in-sample MAE of a ridge fitted on the same training seasons, which favours flexible settings. | Backlog. M12 chooses hyper-parameters on validation seasons instead. |
| 8 | M11 README / `units_backtest.py` | risk | M12's validation seasons (2020-2022) were part of M11's test window, so they have been looked at before. 2023-2025 have too (M10 and M11 test windows). | Documented in MODEL.md. M12 makes no choice by looking at 2023-2025 results; they are scored once, at the end. |
| 9 | `src/betdata/sources/nflverse.py:109-165` (games.csv) | risk | games.csv also has `temp`, `wind` (observed at the game), `home_qb_id`/`away_qb_id` (who actually started) and `result`/`total`. None is parsed today. They are all post-game facts. | M12 rule: never used as inputs. The as-of harness checks that no feature reads them. |
| 10 | `src/betdata/analysis/backtest.py:349-372` | risk | Bets are graded at the closing line and closing price, so a backtest "bet" has zero closing-line value by construction. A live bet placed earlier can't be assumed to get the close. | Documented. M12 grading (`betdata grade nfl`) grades live bets at the stored prediction-time line and reports CLV separately. |
| 11 | `tests/conftest.py` | risk | Nothing stops a test from making a real network call. | **Fix now** (CI issue): autouse guard that fails any socket connection to a non-local host. |
| 12 | (repo) | risk | No CI: green on one machine only. | **Fix now** (CI issue). |
| 13 | `src/betdata/analysis/units_backtest.py:28`, `report.py:340-355` | cleanup | Private helpers (`_pct`, `_span`, `_summary_line`) imported across modules. | Backlog. |
| 14 | `src/betdata/analysis/dataset.py:112-115` | cleanup | Comment correct but subtle: rows iterate by book_id descending so the lowest book_id is written last and wins. | No change. |
| 15 | `src/betdata/analysis/units.py:121-122` | ok (checked) | Walk-forward groups by US-Eastern date; that day's results are learned only after all of that day's predictions. Not a leak (conservative). | No change. |
| 16 | `src/betdata/analysis/units.py:183-190` | ok (checked) | Questionable/Doubtful play rates use only training seasons. | No change. |
| 17 | `migrations/versions/0005_closing_lines_view.py` | ok (checked) | `v_closing_lines` prefers a source's own close and falls back to the last poll at or before kickoff. Correct as the close; **must not be used as a model input** (M12 rule). | No change. |
| 19 | `src/betdata/analysis/context.py:105-127` (data, found while building M12 group 1) | **bug (look-ahead)** | nflverse's weekly rosters for 2013-2015 are one end-of-season snapshot copied into every week: only ~7% of players ever change status, and starters marked RES (IR) or TRD (traded) still logged snaps in those weeks. M11 trains on these, so a player's future IR stint counts as "missing" months early. From 2016 the files really are weekly (INA game-day inactives appear from 2019). | **Fixed (issue #2 follow-up)**: statuses before 2016 are never read (the files are still used to map player ids). M12 applies the same rule. |
| 18 | Open-Meteo, aussportsbetting.com, sportsbookreviewsonline.com | risk (environment) | All three hosts are refused by this machine's network proxy. Stored weather forecasts and historical opening lines can't be collected from here. | Logged in BACKLOG.md. Weather and market features are built and tested on fixtures, then used live where data exists. |

## Fix results (issue #2)

#1-#3 fixed: injury rows carry nflverse's publish time (`report_date`); at M11's prediction
point (the final injury report, `nfl/points.py`) only rows published by then count, and roster
status comes from the team's latest roster before the game week. Existing rows were filled
with `betdata nfl backfill-report-dates` (79,818 rows from 18 archived files). The 2025-2026
files have no `date_modified` column, so those backfilled rows follow the documented rule:
assumed to be the final report.

Elo (M10) doesn't use injuries or rosters: unchanged (10.18 vs 9.81, 990 bets 492-474-24).

M11 `betdata backtest nfl --model units` (train 2013-2019, test 2020-2025, 1,693 test games):

| | before (leaky) | after |
|---|---|---|
| Margin MAE, units+missing, test | 10.09 | 10.11 |
| Margin MAE, units+missing, train | 10.25 | 10.28 |
| Total MAE, units+missing, test | 10.65 | 10.65 |
| Spread threshold chosen on train | 4 pts | 2 pts |
| Spread bets, test | 155-137-1 (53.1%, +2.2% ROI) | 405-403-13 (50.1%, -3.3% ROI) |
| Total bets, test (4 pts) | 238-210-7 (53.1%) | 235-207-7 (53.2%) |
| QB out, margin points | -2.80 | -3.26 |

**Second fix (REVIEW.md #19, pre-2016 roster snapshots)**, same backtest:

| | after #1-#3 | after #19 too |
|---|---|---|
| Margin MAE, units+missing, test | 10.11 | 10.15 |
| Margin MAE, units+missing, train | 10.28 | 10.31 |
| Total MAE, units+missing, test | 10.65 | 10.66 |
| Spread bets, test | 405-403-13 at 2 pts | 495-511-17 at 1.5 pts (49.2%) |
| Total bets, test (4 pts) | 235-207-7 (53.2%) | 214-199-7 (51.8%, -0.4% ROI) |
| QB out, margin points | -3.26 | -3.57 |

Together the two roster leaks were worth about 0.06 points of M11's margin error. With both
fixed, neither of M11's betting records beats break-even on the test seasons.

The first leak alone was worth about 0.02-0.03 points of margin error. M11's "53.1% on spreads" partly
came from it: with the leak removed, training chooses a 2-point threshold that loses on test.

Bet count (M13 Part A): the spread threshold is re-chosen on training ROI after every fix
(4 → 2 → 1.5 pts), which is why spread bets went from 293 to 1,023. The rule is M11's,
unchanged. With the threshold held at 4 pts, the corrected M11 makes 304 test bets,
156-145-3 (51.8%, -0.1% ROI). See MODEL.md, "M13 Part A".

## M14 findings (bugs in shared code)

| # | Where | Severity | Finding | Action |
|---|---|---|---|---|
| M14-1 | `src/betdata/runner/hourly.py` (`run_hourly`) | bug | M13 #58 set the run's as-of time to `max(now, utcnow())`. For a run of a past instant (`runner run --at`, and the runner tests), that jumps to today's wall clock, so "last good data" and predictions were stamped with the wrong time. `test_failed_run_keeps_last_good_page_with_warning` passed only before 16:05 UTC. | **Fixed**: a run within 30 minutes of the wall clock still uses `max(now, utcnow())`; a run of a past instant moves forward by its elapsed time only. |
| M14-2 | `src/betdata/sources/sportsdataverse.py` (`parse_schedule`) | bug | NBA games got their season from the calendar (`season_for`: August onwards is the next season). The 2020 bubble games (July 30 - October 11, 2020) were labelled 2020-21 instead of 2019-20. | **Fixed**: NBA rows use the file's own `season` column (the year a season ends) minus one. Test: `test_bubble_games_keep_their_season`. |
| M14-3 | `src/betdata/sports/__init__.py` (`Card`), `src/betdata/page/build.py` (`card_html`) | interface addition | Part F asks for back-to-back and rest flags on NBA cards; `Card` had no field for them. | **Added**: `Card.notes` (empty by default) rendered as one line under "Likely out". NFL cards set none, so the NFL page is byte-for-byte unchanged (test `test_nfl_page_unchanged_by_notes`). |
| M14-4 | `src/betdata/nba/features.py` (`availability`) | bug (found in live cards, after the first final-test run) | Before a team's first game of a season, the league state still holds last season's end-of-year absence streaks, so on opening night a healthy star who sat the last 15 games of last season was given a 15% chance to play. It affected the first game of every season in training, validation and test alike. | **Fixed** (`streak_for`): streaks count within the game's season. The frozen design (groups, penalties, thresholds, blend, calibration) was kept unchanged; coefficients were refitted and the test re-scored with `--again`. MODEL.md reports both runs (they differ by 0.001 in MAE). |
| M14-5 | `src/betdata/nba/state.py` (`_new_season`) | limitation (found in live cards) | When the first game of a season is fed, every team's roster is cleared, so a team that has not played yet has an empty rotation: no availability or lineup features for its first game (training, validation, test and live alike). | **Tried and rejected on validation**: carrying last season's rosters over (with streaks reset) raised validation MAE (10.516 → 10.520) because departed and retired players then lingered as absences. Reverted; BACKLOG.md (a preseason roster feed). The live card still lists every player on the injury report. |
| M14-6 | `src/betdata/nba/data.py` (`load_injuries`) | bug | The report-name → player map dropped any name seen with more than one team in box scores, so players who changed teams (e.g. Brandon Ingram, Max Strus) never matched. | **Fixed**: a name is ambiguous only if it belongs to two different player ids. Validation and test numbers unchanged to 4 decimals; the fit was refrozen. |
| M14-7 | `src/betdata/sources/espn_nba.py` | bug (live) | ESPN now names the provider "Draft Kings" (with a space), so no DraftKings line was stored. | **Fixed**: provider names are compared ignoring case, spaces and punctuation. |

## M15 NFL findings

| # | Where | Severity | Finding | Action |
|---|---|---|---|---|
| M15-1 | `.github/workflows/hourly.yml` (state cache), `config/sports.yaml` | gap (live m15) | The hourly state cache keeps `data/betdata.sqlite`, `data/derived`, `data/site_last_good` and `data/reports` only. Play-by-play is downloaded for the current season by the daily 7am job, and the nflverse files (depth charts, participation, PFR, NGS) are not scheduled at all. So the runner never has the three seasons of files that m15's live inputs need. | **Resolved**: m15's inputs are now compact per-season tables in `data/derived` (about 0.8 MB, kept by the state). The daily 7am run downloads this season's PFR and NGS (participation once published) and refreshes the tables (`nfl-inputs15`). The one-off `nfl-m15-bootstrap` workflow builds the earlier seasons. `live15.inputs_ready` still refuses incomplete inputs. No raw files are added to the cache. |
| M15-3 | `src/betdata/nfl/props/live.py` (`predict`) | performance | Props were computed one line at a time: volumes, terms and factors were rebuilt for every line. With about 1,700 NFL lines a week, that took 71 s of an hourly run. | **Fixed**: each market's lines are computed together (`factors_many`). Identical output on 1,670 lines; 9 s. |
| M15-2 | `src/betdata/nfl/predict.py` (`store_picks`) | latent bug | Every model other than m12 was stored with `params.design = config/nfl_model_wx.json`, so m15 rows would have named the weather design. | **Fixed**: the design file comes from the model name (m12, m13w, m15), and anything else keeps the old label. m12 and m13w rows are unchanged. |
| M15-3 | `sources/espn_props.py` history, `nba/props/select.history_lines`, `props/grade.closing_lines` | data trap | For a finished game, ESPN's propBets `current` quote is usually the book's last **in-game** line, not the pre-game close: 99% of the stored history "close" rows were quoted after tip (2022-23: 138,002 of 138,662), and books disagreed by up to 10 points (DraftKings moved a 14.5 points line to 6.5 during the game). Backtesting at them gave Platt slopes near 0 and fake +17% ROIs. | **Fixed**: a history `close` counts only if quoted before tip (backtest and grading); the backtest uses the opening line (pre-game, posted before both prediction points). Live closes are the last poll before tip, unaffected. |
| M15-4 | `sources/nflverse_data.store_pbp` | bug | A play-by-play file whose checksum matched the last version recorded in `data_files` was never written, even when the file was missing on disk. On the hourly runner the database is restored but the files are not, so `nfl-m15-bootstrap` downloaded 2024-2025 play-by-play and dropped it (`inserted=0`), and m15 skipped with "play-by-play for 2025 missing". | **Fixed**: a recorded file missing on disk is written again (no new version row). Test `tests/integration/test_pbp_restore.py`. |
