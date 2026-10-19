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
