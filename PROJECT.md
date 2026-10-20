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
| 2026-10-19 | M13: the page is hosted on GitHub Pages. The owner made the repository public, because Pages on a private repository needs a paid plan. | No servers or paid hosting. The link is public to anyone who has it (see below). |
| 2026-10-19 | Runner state lives in the Actions cache, not a data branch or release asset. | It is private to the repository's workflows; a branch or asset would republish raw API responses publicly. A lost cache is rebuilt from free sources. |
| 2026-10-19 | No hourly refit. M12's 2013-2025 live fit is committed in `config/fits/`. | The completed seasons change once a year, so an hourly refit changes nothing. The committed fit keeps the coefficients identical after a state rebuild. |
| 2026-10-19 | Moneyline: no calibration applied. | The one Platt fit on validation (slope 1.31) shows the model is not overconfident (MODEL.md). |
| 2026-10-19 | Weather version `m13w` (M12 + `wx` in the margin model) ships beside frozen M12. | Validation kept `wx` for the margin only (MODEL.md). |
| 2026-10-19 | No new runtime dependencies in M13. Playwright is used only in the `verify-page` workflow (pip, on the runner) and for local screenshots (`uv run --with`). | The page is static HTML/CSS/JS written by Python. |
| 2026-10-20 | Hourly workflow has a backup trigger at :30, and a slot that already ran successfully is skipped. | GitHub dropped 6 of 7 top-of-hour scheduled runs on the first evening. |
| 2026-10-20 | Clock workflow: a self-redispatching job that starts each hourly run. | GitHub's cron kept dropping almost every tick, including the :30 backups. This stays inside "Actions + Pages only", at the cost of an idle runner, which is free on a public repository. |

## M13: the hourly page

**Page:** https://piratekinginc.github.io/test-m11/ (GitHub Pages, built by `.github/workflows/hourly.yml`).

> **The link is unlisted, not private.** A GitHub Pages site is public to anyone who has the URL unless the repository is on GitHub Enterprise with access control. Pages on a private repository also needs a paid plan, so the repository was made public for M13. Anyone can read the code, the workflow logs and the page. API keys stay in GitHub Secrets and are never committed, printed or archived; `core/http.py` redacts them from the raw archive.

**Schedule and DST.**
- The page updates every hour on the hour, 7:00am-10:00pm America/New_York: 16 runs a day.
- GitHub's cron is UTC, so the workflow fires at minute 0 of every hour from 11:00 to 03:00 UTC (`0 0-3,11-23 * * *`, 17 a day).
- A backup trigger at :30 in the same hours (`30 0-3,11-23 * * *`) covers the scheduled runs GitHub drops under load: on the first evening it dropped 6 of 7. A slot that already has a successful scheduled run is skipped, so each slot runs once, at :00 when GitHub starts it and otherwise at :30.
- **GitHub's cron proved unreliable here.** Between 21:00 UTC on Oct 19 and 13:00 UTC on Oct 20 it started 2 of about 40 ticks, :00 and :30 together, one of them two hours late. So the page also has a **clock** (`.github/workflows/clock.yml`):
  - **How it works:** one job sleeps until just after each hour, dispatches the hourly run if that hour is 7am-10pm Eastern (recorded as trigger `clock`), then dispatches itself again. Dispatches made with the workflow token do start runs.
  - **One clock at a time:** a newer clock cancels an older one.
  - **Restart:** a 6-hourly cron restarts the chain if it ever breaks.
  - **No doubles:** cron and clock share the per-slot dedupe, so a slot runs once.
  - **Start it:** manually once, via Actions → clock → Run workflow.
- 7am-10pm Eastern is 11:00-02:00 UTC in EDT and 12:00-03:00 UTC in EST. A tiny `gate` job (standard library only, about 10 s, no install) checks the Eastern hour and stops the one extra run a day.
- A run belongs to the hour it was due in, because GitHub often starts scheduled runs minutes late.
- `tests/runner/test_window.py` checks exactly 07:00 ... 22:00 Eastern on Oct 31, Nov 1 and Nov 2, 2026 (DST ends Nov 1) and around the March change.
- Manual runs (`workflow_dispatch`) ignore the clock.

**What a run does** (`betdata runner run`):
1. If the database has no history, rebuild it (below).
2. Run the NFL data jobs due this hour (`config/sports.yaml`):
   - every run: nflverse schedule/lines/scores, injuries and weekly rosters, plus SharpAPI if its key is set, and The Odds API only within 60 minutes of a kickoff;
   - every 4 hours (7, 11, 15, 19 ET): weather forecasts;
   - Tuesdays at 7am: archived forecasts for finished games (weather model history);
   - the 7am run only: play-by-play, team stats, snaps and `nfl derive`.
3. Predict every scheduled game in the next 8 days (stored change-only), back-filling a missed final-report or prekick point.
4. Grade finished games: spread and moneyline (totals are graded but stay in the CLI).
5. Record the run in `runner_runs` and build the page.

**No hourly refit.** The model is the frozen M12 design fitted on every completed season (2013 through last season). That set changes once a year, so the fit is computed once and stored in `data/derived/live_fit_*.json`; hourly runs read it back. A refit happens only when a new season starts (the first run after the season rolls over) or after a state rebuild. Refitting hourly would cost minutes and change nothing.

**State.**
- **Where:** the Actions cache holds the SQLite database, `data/derived` (fits, training-season parameters, cached features), the last good page and `data/reports`. That is about 57 MB compressed, and saving or restoring takes about 3 s each.
- **Save and restore:** each run restores the newest entry (`betdata-state-v1-*`) and saves a new one, even after a failure. GitHub evicts the oldest entries beyond 10 GB.
- **Why the cache:** it is private to the repository's workflows. Release assets and data branches would be public, which would republish raw API responses.
- **If the cache is gone** (evicted after 7 days unused, or deleted), the next run finds no history and rebuilds it from free nflverse files: schedules, team stats, snaps, injuries, rosters and play-by-play for 2010 onward, then derive. The run is marked `rebuilt`, and the page says the data was rebuilt and that stored predictions and grades from before then are gone.
  - **Measured:** the first live run had no cache, and the rebuild-and-predict run took 85 s (whole job 2 min 10 s).
  - **Coefficients:** M12's coefficients are the committed fit, so they are identical after a rebuild. The weather version is refitted once from the rebuilt history; its validation design is fixed.
  - **Weather history:** the archived weather rows are restored by the `weather` workflow or the weekly job.
- **Testing it:** run the workflow with `drop_state: true`.

**Failures.**
- **Overlap:** `concurrency: hourly` (no cancelling) means runs never overlap.
- **Data job failures:** a failing data job (a source down, a key missing) is recorded and shown on the page; the run carries on.
- **Run failures:** if the run itself fails (`force_fail` tests this), the workflow republishes the last good page with `status.json` marking the failure, and the job is marked failed.
- **Warnings in the browser:** the page checks `status.json` itself and shows a warning when the newest data is more than 2 hours old or the last run failed. So the warning appears even if the workflow stops entirely.

**Secrets.**
- Keys are read only from GitHub Secrets: `ODDS_API_KEY` (or `THE_ODDS_API_KEY`), `SHARPAPI_KEY`, `ODDSPAPI_KEY` and `CFBD_API_KEY`, the names in `.env.example`.
- A missing key skips that source. The page lists the skipped sources and shows the source and book behind every market number.
- As of M13, **none of these secrets is set**: `ODDS_API_KEY`, `SHARPAPI_KEY`, `ODDSPAPI_KEY` and `CFBD_API_KEY` are all missing, so all lines come from nflverse.

**Open-Meteo licence.** Weather comes from Open-Meteo, which is free for **non-commercial use only**, with data under CC BY 4.0. The page carries the required attribution. **Commercial use of this page (or of the weather model's output) would need a paid Open-Meteo licence.**

**Quota over 30 days** (`betdata scheduler plan --runner --days 30`, 2026-10-19, real NFL schedule; worst-case simulation in `tests/runner/test_quota_month.py`):

| Source | Runs | Units in 30 days | Free limit (after safety margin) | Verdict |
|---|---|---|---|---|
| nflverse (schedules, injuries, rosters hourly; pbp, stats, snaps daily) | 479 + 90 | 0 | no quota | OK |
| SharpAPI (hourly, if keyed) | 479 | 479 (~16/day) | 3,000/day | OK |
| The Odds API (one snapshot 0-60 min before each kickoff window) | 24 | 72 | 450/month (500, 10% held back) | OK |
| OddsPapi (Sunday early and Mon/Thu prime time) | 13 | 13 | 225/month (250) | OK |
| Open-Meteo forecasts (every 4 h, outdoor games in the next 7 days) | 120 | 1,200 (~40/day) | 5,000/day (10,000, 50% held back) | OK |
| Open-Meteo archived forecasts (weekly; new finished games only) | 4-5 | ~70-110 a week once backfilled | 5,000/day | OK |

The Odds API cannot support hourly polls (16 x 3 credits x 30 days = 1,440 against 500), so it keeps its pre-kickoff schedule. Hourly lines come from SharpAPI when keyed, and from nflverse otherwise.

On this machine's database, `plan --runner` charges the weekly archive job for every 2022+ game, about 2,900 units a run, because the local database has no archived rows; Open-Meteo is unreachable from here. That is still under the daily 5,000. In Actions, the backfill is done, so the job only fetches the week's new games.

**Runtime and Actions minutes** (measured on GitHub's runners, 2026-10-19/20):

| | Wall time | Billed (each job rounded up to a minute) |
|---|---|---|
| Typical scheduled run: gate 4 s + run job 46 s (restore 3 s, run 21 s, save 3 s, deploy 7 s) | ~50 s | 2 min |
| Off-hours tick (gate only) | ~4 s | 1 min |
| Backup :30 trigger when :00 already ran (gate + skip) | ~35 s | 2 min |
| First run after state loss (rebuild) | ~2 min 10 s | 4 min |

- **Typical run:** about 50 s, well under the 3-minute budget.
- **The clock** is one runner job sleeping about an hour per tick, so it bills about 60 minutes an hour, roughly **43,000 minutes a month**. That is free on a public repository, but if the repository goes private this far exceeds any free plan: remove `clock.yml` and rely on cron, accepting dropped hours.
- **Monthly projection for the hourly runs themselves:**
  - 16 slots × 2 triggers × 2 min × 30 days = 1,920 min;
  - plus 2 off-hours ticks a day = 60 min;
  - about **1,980 billed minutes a month**.
- **Cost:** standard runners are free and unmetered on a public repository. If the repository goes private again, the free plan's 2,000 minutes a month would be used up. Drop the backup trigger first (about 1,020 minutes a month), since it only matters when GitHub drops the :00 run.
- **Scheduled workflows stop after 60 days without repository activity.** GitHub's rule for public repositories. Re-enable them in the Actions tab, or push a commit.


## M14: NBA

### Dependencies

* **pdfminer.six** (MIT; pulls `cryptography` and `charset-normalizer`): reads the official NBA
  injury report PDFs with each text line's position, which the report's table layout needs
  (`src/betdata/nba/injury_pdf.py`). It is the only new runtime dependency of M14.
