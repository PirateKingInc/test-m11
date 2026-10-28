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
| 2026-10-21 | M15 NBA lineup stints in `nba_stints` (migration 0018). sportsdataverse play-by-play files are not archived in `raw_responses` (only size and sha256); the runner's state holds stints for the last three seasons only. | Each file is ~20 MB and free to fetch again; the full history (`betdata backfill --source sportsdataverse --sport nba --dataset pbp`) is ~45k rows a season and only needed where the RAPM fit runs. |
| 2026-10-21 | M15 nflverse files (participation, FTN, PFR, depth charts, player stats, NGS) are kept as parquet under `data/nflverse/` by source `nflverse_extra`, CLI/backfill only, outside the Actions cache. | 53 MB in all; nothing in the hourly run reads them yet. Features derived from them should go into database tables (cached), as play-by-play does; the current season's files can then be fetched by one daily `refresh` job. |
| 2026-10-21 | m15 and NFL prop live inputs are compact per-season tables under `data/derived` (kept by the runner state), refreshed by the daily 7am job `nfl-inputs15`; earlier seasons built once by the `nfl-m15-bootstrap` workflow. | Caching raw play-by-play and nflverse files would add about 100 MB to the state; the tables are about 1 MB and give identical features (see "NFL M15 on the hourly runner"). |
| 2026-10-21 | M15 NFL props (`nflp15`): the hourly NFL run fetches this season's nflverse player stats and depth charts once a day (`nfl-player-stats`, `nfl-depth-charts`). The prop panel caches its per-season player-game rows and the ESPN→gsis id map under `data/derived` (kept by the runner state). Every NFL prop market is research-only. | Prop inputs and grading need weekly player stats; prop lines carry ESPN athlete ids, and the depth charts map them to gsis ids. No NFL prop lines exist for validation, so no betting backtest or threshold is possible (MODEL.md, "M15 NFL player props"). |
| 2026-10-22 | **Season record rule (M15.1):** the page's season record counts one pick per game and market, the model's last prediction before the start, whenever its edge cleared the market's threshold (the flag rule), whether or not a flag was shown. Only the version the page uses is in the table; background versions are in a collapsed section. Under 100 picks the ROI reads "too few to judge". | The record counted every prediction point of a game, up to three times (NBA moneyline read 0-20 from 7 games). One rule for spread and moneyline, stated above the table. |
| 2026-10-22 | **Scope change requested by the owner (M16):** alerts are now in scope. M16 adds a fast injury poller on the clock workflow, two closing-line-value signals, push alerts via ntfy.sh (topic in the `NTFY_TOPIC` secret), paper bets for every alert, and a lag study. No model, sport or bet-placement change. | Every model phase lands near the close but not past it; M16 targets closing-line value directly. Earlier milestones listed alerts as out of scope; this is a deliberate change. |
| 2026-10-27 | **M17:** a market-free NFL engine (plays → pass/run → dropbacks → targets/carries → efficiency → drives → 10,000 simulations) ships as new versions `nfle17` (sides) and `nflpe17` (props) beside the current ones. Sides: train 2016-2019, validate 2020-2022, test 2023-2025 (repeat look). Props: thresholds on 2024 ESPN lines, test 2025. The coordinator table comes from Wikipedia (CC BY-SA, attributed per row); Pro-Football-Reference is not scraped. | MODEL.md "M17". The current models lean on team-level ratings; the engine tests whether a play-level build, never shown a line, finds what the market misses. |
| 2026-10-28 | **M17 result:** nfle17 and nflpe17 run hourly as info only. No side flags: the 4-pt threshold chosen on 2020-2022 lost on 2023-2025, ROI −8.3%. No prop flags: no market's range clears zero. The page stays on m13w: its test margin MAE is 10.27 against m13w's 10.06. ESPN fantasy is dropped on the Disney Terms of Use; Sleeper and FantasyPros ECR are benchmarks only. The coordinator prior and ff_opportunity passed their rules after the freeze and wait for the next engine version. | MODEL.md "M17 test" and "M17 F results"; `reports/M17_REPORT.md`. |
| 2026-10-28 | **NFL lines:** NFL picks are taken, shown and graded at DraftKings/FanDuel only, the best of the two at pick time and at that book's price. CLV is measured against Pinnacle's pre-game close, else DK/FD's. All stored NFL picks were regraded. Requested by the owner after the m13w 0-8 audit, which found no sign flip. | MODEL.md "NFL lines". Picks had been graded against whichever book was polled last, often offshore. |

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

### NBA sources (all free; checked from an Actions runner)

| Data | Source | Hourly job | Quota |
|---|---|---|---|
| Schedule, tip times, scores, free DraftKings line | ESPN scoreboard API (`espn_nba scoreboard`) | every run, Oct-Jun | none (polite: one request per 0.5 s) |
| Box scores of newly finished games | ESPN game summary (`espn_nba boxscores`) | every run | none (one call per finished game) |
| Official injury report (latest PDF) | `ak-static.cms.nba.com` (`nba_injury report`) | every run with games in the next 2 days | none (one request per second; a burst of ~2,000 was refused) |
| Lines from DraftKings and FanDuel | SharpAPI | every run with games in the next 2 days | needs `SHARPAPI_KEY` |
| One board before the evening tips | The Odds API | 18:00 ET | needs `ODDS_API_KEY` |
| One Pinnacle board | OddsPapi | 17:00 ET | needs `ODDSPAPI_KEY` |
| History: schedules, box scores 2015-16 on | sportsdataverse release files | rebuild only | none |
| History: injury reports, opening and closing lines | league PDFs, ESPN core odds API | `nba-history` workflow (manual) | none |

**Blocked from Actions:** cdn.nba.com (403) and stats.nba.com (no answer). Neither is used.

**ESPN's public APIs** are unofficial and undocumented. They are used the way the M7 sportsdataverse files already use ESPN data: free, keyless, and at a polite rate. If ESPN closes them, the NBA page loses its free line and live scores; the sportsdataverse files remain for scores with up to a day's delay.

**State.** The live runner loads only the last three seasons plus the current one (`LIVE_SEASONS`). A lost cache is rebuilt from the sportsdataverse files and ESPN (`NbaSport.bootstrap`), like NFL. The injury-report and line history that feed the backtest are not needed live. They live on the `nba-history` branch and load with `betdata nba load-history`.

**Adding NBA to an existing state** (`nba-bootstrap` workflow, run once before NBA went live) loads that history without marking a rebuild. Otherwise the first NBA run would have shown the "saved data was lost" notice for 48 hours.

### Quota over 30 days, NFL + NBA

Measured with `betdata scheduler plan --runner --days 30`, 2026-10-20, on the real schedules of both sports. NBA has games on 26 of the 30 days. The worst case for both sports is simulated in `tests/runner/test_quota_month.py`.

| Source | NFL units | NBA units | Total | Free limit (after margin) | Verdict |
|---|---|---|---|---|---|
| SharpAPI (hourly, if keyed; one request per sport) | 479 | 479 | 958 (~32/day) | 3,000/day | OK |
| The Odds API (NFL pre-kickoff; NBA one 18:00 ET board on game days) | 72 | 87 | 159 | 450/month | OK |
| OddsPapi (NFL windows; NBA one 17:00 ET board on game days) | 13 | 29 | 42 | 225/month | OK |
| ESPN NBA (scoreboard: 4 day-requests a run, yesterday to +2 days; box scores: one per newly finished game) | - | ~1,900 + ~200 requests | - | no quota | OK |
| NBA injury report PDF (1 per run with games within 2 days) | - | ~450 requests | - | no quota | OK |
| Open-Meteo, nflverse | as in M13 | - | - | - | OK |

None of the keyed sources has a secret today, so they are skipped and cost nothing. The table shows what they would use if keyed.

### Runtime with NFL + NBA

Measured on GitHub's runners, 2026-10-21, with NBA active.

| Run | Run step | Whole run job |
|---|---|---|
| NFL only (last runs before NBA went live) | 18-21 s | ~45 s |
| NFL + NBA, regular hourly run | 34-35 s | ~55 s |

- **The NBA share** is about 15 s: load the last three seasons into the as-of store, predict the next 2 days and fetch the injury PDF.
- **Budget:** a run is well under 3 minutes, and billed minutes are unchanged at 2 a run.
- **Rebuild:** a run that rebuilds a lost state with NBA (sportsdataverse files for 2015-16 on, then ESPN for this season) is the one exception. It is a one-time cost, and history loading is separate (`nba-bootstrap`).

## M15: new side variables and player props

### NFL M15 on the hourly runner (state size and runtime)

- **State:** the m15 and NFL prop live inputs are compact tables in `data/derived`, which the state already keeps. No play-by-play or nflverse file is cached.
  - m15 (2024-2026): team-games, kickers, and depth charts kept only when they change. About 0.75 MB.
  - Props (2025-2026): player-games, team-games and the ESPN id map. About 0.33 MB.
  - Together about 1.1 MB.
- **Daily (the 7am run):** downloads this season's play-by-play, player stats and depth charts, plus PFR pass and NGS passing (about 1.5 MB), and participation once nflverse publishes it. Then `nfl-inputs15` rebuilds this season's tables, which took about 15 s locally.
- **Hourly:** m15 adds about 6 s, because it shares the M12 store with m12 and m13w. Props add about 9 s with about 1,700 lines. Both were measured locally on a copy of the runner's data with no raw files on disk.
  - Locally the whole NFL predict step (store load 19 s, m12 + m13w 11 s, m15 6 s, props 9 s) takes about 45 s.
- **One-off:** `nfl-m15-bootstrap` (manual, concurrency group `hourly`):
  - restores the state;
  - downloads 2024-2026 play-by-play and nflverse files into its workspace;
  - runs `betdata nfl live-inputs`;
  - saves the state.
  - Run it once after merging. Run it again after a lost-state rebuild: until then m15 is skipped, and props read whatever player stats the 7am run has fetched.

### Data licences and attribution (checked 2026-10-21)

| Dataset | Licence | Attribution |
|---|---|---|
| nflverse-data (pbp, participation, NGS, PFR advanced, depth charts, player stats) | CC-BY 4.0 | "Data: nflverse" (already on the page since M12) |
| FTN charting via nflverse | CC-BY-SA 4.0 | "Charting data: FTN Data via nflverse", on the page wherever FTN-derived numbers are used |
| sportsdataverse-data (NBA play-by-play, box scores) | MIT (repository); underlying data from ESPN | "NBA data: sportsdataverse / ESPN" |
| ESPN prop lines (core API) | none published; unofficial endpoint | Lines are shown with their source and book. Raw responses stay in the private Actions cache and are never republished. Requests stay at ≤1 a second, with backoff on 429 or empty payloads. |

M15 needs no paid source and no new API key.

### M15 quota and runtime proof (NFL + NBA sides and props, measured on the runner)

**Measured hourly runs, 2026-10-21, automatic (clock):**

| Run (UTC) | What ran | Run step | NFL predict | NBA predict | nba-props lines | nfl-props lines |
|---|---|---|---|---|---|---|
| 21:00 | sides + prop lines (no prop design yet) | 84 s | 20 s | 20 s | 38 s | 2 s |
| 22:00 | + NBA and NFL prop predictions | 95 s | 22 s | 29 s | 37 s | 2 s |
| 23:00 | + m15, NFL props four-hourly refresh (19:00 ET) | 126 s | 25 s | 30 s | 37 s | 29 s |

- **Whole job:** about 30 s more than the run step (restore and save state, deploy), so about 2.0-2.6 minutes. That is under the 3-minute budget.
- **Largest cost:** the NBA prop lines, at one request a second (about 37 requests for a slate). Distant NFL games are refreshed only every 4 hours (7, 11, 15, 19 ET) to keep the other runs short.
- **The 7am run:** it adds the daily NFL refresh (play-by-play, player stats, depth charts, PFR and NGS, then `nfl-inputs15`, about 15 s), and is projected at about 2.5 minutes. If it ever passes 3 minutes, the first cut is to refresh NBA lines for games more than 24 hours away every 4 hours instead of hourly; no market would be dropped.
- **Never refit hourly:** every M15 model reads a frozen fit under `config/fits/`. RAPM uses the snapshot of the latest Monday, about 1 s to fit from the stints.

**Quota over 30 days:**
- **ESPN prop lines** (core API, keyless, no published quota): about 37 requests a run for NBA × 16 runs ≈ 600 a day, plus NFL ≈ 30 a run on four-hourly runs and fewer otherwise. Always at ≤1 request a second, with backoff on 429 and 404 treated as "no props".
- **History backfill** (`props-history`, manual): about 5,000 requests a season at 1 a second, one season at a time.
- **nflverse and sportsdataverse files:** GitHub release downloads, no quota.
- **Keyed sources** (SharpAPI, The Odds API, OddsPapi) are unchanged from M14: SharpAPI 958 of 3,000 a day, The Odds API 159 of 450 a month, OddsPapi 42 of 225 a month. None is keyed today, so they use nothing.

**State:** the Actions cache entry is about 99 MB compressed (93 MB before M15). That covers the lineup stints for 2024-25 to 2026-27, prop lines, prop predictions and the NFL m15/prop tables (about 1 MB).

**Dependencies:** M15 adds no new runtime dependency. RAPM uses `scipy.sparse`, which is already installed with scikit-learn.

## M16: alerts and closing-line value

### Line storage (checked 2026-10-22, issue #157)

M16 grades every alert against the pre-game close, so every line we poll must still be there after tip. Read in the code:

- **Game lines** (`odds_snapshots`): every live loader writes through `core/odds_loader.load_odds`: the ESPN NBA scoreboard (`espn_nba`), SharpAPI, The Odds API, OddsPapi, CFBD and nflverse. It only **inserts**. A row is added when a selection's line, price or status differs from its previous row, and a selection that leaves the board gets a `removed` row. The unique key is (source, event, book, market, outcome, kind, `fetched_at`), so each poll is a new row and re-loading the same response adds nothing. ESPN's per-game odds history (`espn_nba` odds job, `nba/history.load`) also only inserts.
- **Polls with no change** add no row, by design. The poll itself is still recorded in `ingest_runs` (and the response in `raw_responses`, unless it is byte-identical to the last one). So "unchanged as of T" can be answered, and the latest row before tip is the line at tip (`v_closing_lines`).
- **Prop lines** (`prop_lines`): `sources/espn_props.store_rows` (live polls) and `props/history.load` (history files) only insert, change-only per (game, player, market, book, kind), unique on `fetched_at`.
- **No code deletes or rewrites a price, line or time.** The only `UPDATE` on `odds_snapshots` is `betdata relink`, which fills a null `game_id` once a team name is mapped. `betdata games split-merged --relink-odds` (M16-1) can also re-point `game_id` on odds and prop rows; that is opt-in. Nothing else updates or deletes a `prop_lines` row.

`betdata lines coverage --sport nba|nfl --days N [--strict]` lists, for games from the last N days and the next two, per source and book and market:
- the opening line (a source's own opening, else the first poll);
- the current line;
- the pre-game close (a source's own close, else a poll at or before tip);
- the median minutes from that source's last successful run to tip;
- for props: open, close and the number of poll rows.

It exits non-zero only with `--strict`, when a started game has no close or an upcoming game has no current line in a market.

### M16 sources (free plans, measured live from an Actions runner, 2026-10-22)

| Source | What the free plan returned | Cost per call | Pinnacle | Props | Verdict |
|---|---|---|---|---|---|
| **SharpAPI** (`SHARPAPI_KEY`) | DraftKings and FanDuel only, 60 s delay, 12 requests a minute. Each call returns at most **200 rows**, with no paging metadata, sorted by event start; most spread/total rows are alternate lines unless `is_main_line=true` is sent. `event_id` filters to one game. | 1 request (rate limit only, no daily cap) | no (tier "free": `max_books 2`) | yes: `player_points`, `player_rebounds`, `player_assists`, `player_made_threes`; NFL `player_receiving_yards`, `player_rushing_yards`, `player_receptions` | **Used**: DK/FD prices for Signal 1, Signal 2 and the lag study. Also fixed a live hourly bug: alternates stored as market lines (REVIEW M16-3). |
| **The Odds API** (`ODDS_API_KEY`) | Pinnacle with `bookmakers=pinnacle` (region eu): h2h, spreads and totals for 14 of 41 NBA and 14 of 28 NFL listed events (the near-term ones). Pinnacle player props per event (`/events/{id}/odds`). | 1 credit per market (3 for the main board; 1 per prop market per event); `/sports` is free and reports the balance. 500 credits a month. | **yes** | yes, but 1 credit per market per event (unaffordable at scale) | **Used** as the sharp reference for Signal 2, inside a budget kept below the hourly job's reserve. |
| **SportsGameOdds** (`SPORTSGAMEODDS_API_KEY`) | Tier "amateur": 10 requests a minute, **2,500 objects (events) a month**. Books in the payload: FanDuel, DraftKings, Bovada, ESPN BET, Caesars, BetMGM (+ a few). | 1 object per event returned (a 5-event call cost 5 of 2,500) | **no** (not in `byBookmaker` for NBA or NFL) | yes (thousands of player odds per event) | **Dropped**: no Pinnacle on the free plan, and SharpAPI already covers DK/FD props without an object cap. |
| **Kalshi** (public, no key) | Open game-winner markets (`KXNBAGAME`, `KXNFLGAME`) plus spread and total series; prices as `yes_bid_dollars` / `yes_ask_dollars` / `last_price_dollars`. Tickers name the date and both teams (`KXNBAGAME-26OCT26GSWDEN-GSW`). | free; spaced 1 a second | - | - | **Used** as a secondary probability (game winner), logged in the lag study. |
| **NBA official injury report** | A new PDF every 15 minutes (`_HH_MM` names), posted about 4 s after the stated time. | free | - | - | **Used** (the M14 parser and loader). |
| **ESPN NFL** (summary `injuries`, event roster) | Per-team injury statuses with dated entries; game-day rosters with `active` flags. | free | - | - | **Used** for NFL status changes. |

### M16 poller (Part C)

- **Where it runs:** the `poll` job of the clock workflow, beside the clock's own `tick` job, each hour until :56 (the next clock run would cancel it at about :00:30).
- **Isolation:**
  - It restores the hourly state as a throwaway copy and never saves it.
  - Everything it learns goes to `data/poller.sqlite`, saved under its own cache key `betdata-poller-v1-*`.
  - The hourly job only reads that file to draw the Alerts section, in a separate step that may fail without touching the page.
  - Before each minute's work it asks the GitHub API whether an hourly run is queued or in progress, and skips that minute if so (no overlap).
  - It exits at once in an hour with nothing to do.
- **Cadence:**

| What | When | Rate |
|---|---|---|
| NBA official report | every 3 min, 10:00-19:30 ET on game days and 16:30-20:30 ET the day before | <= 1 request a second |
| NFL statuses | every 5 min from 120 to 60 min before each kickoff window | <= 1 request a second |
| Lag polls (DK, FD via SharpAPI; Kalshi) | every 3 min (NBA) / 5 min (NFL) for 60 min after a qualifying change | SharpAPI <= 10 a minute |
| Signal 2 boards (Pinnacle + DK/FD) | NBA 17:30 and 18:45 ET on game days; NFL 75 min before each kickoff window | 3 credits a board |
| Closing polls | games with an alert, in the 10 min before tip | |

- **On a qualifying change:**
  - Only that game is re-predicted, with the frozen page models (NBA nba15 + nbap15; NFL m13w + nflp15), just before vs just after the news.
  - Current DK/FD lines are pulled, and Signal 1 is evaluated.
  - Nothing is refitted.

### M16 quota: 30 days of hourly runs plus polling (verification e)

Measured with `betdata alerts quota --days 30` on the live state on 2026-10-23: 28 NBA game days and 27 NFL kickoff windows. Each line is the poller's worst case: every assumption in `alerts/quota.py` is an upper bound, and a CI test checks a worst-case month (`tests/alerts/test_quota.py`).

| Source | Hourly job | Poller (worst case) | Free limit | Verdict |
|---|---|---|---|---|
| The Odds API | 156 credits | 249 credits (Signal 2 boards) | 450 usable of 500 a month | OK (405) |
| SharpAPI | ~32 calls a day | 20,446 calls (peak day 1,580) | 12 a minute; no daily cap | OK: the poller spaces calls 6 s apart (<= 10 a minute) and never polls while an hourly run is queued or running |
| NBA injury report | ~450 | 15,280 (peak day 540) | none published | <= 1 a second |
| ESPN (NFL statuses) | - | 2,052 | none published | <= 1 a second |
| Kalshi | - | 9,312 | public API | <= 1 a second |

Live guard: before each Pinnacle board the poller reads the remaining credits (the free `/sports` call). It skips the board if spending it would leave less than the hourly job's need for the rest of the month (5.3 a day) plus 50.

### Line storage on the live state (issue #157, 2026-10-23)

`betdata lines coverage` on the live state:
- **NFL** (games of the last 7 days): every game has a pre-game close.
  - Only games played after the hourly job began polling on 2026-10-19 have an opening and a current line from polls; earlier ones keep nflverse's close.
- **NBA** (last 3 days plus the next 2): 13 of 16 started games have a pre-game close at every polled book (ESPN DraftKings; SharpAPI DK/FD and The Odds API since 2026-10-22).
  - The 3 without one are opening-night games (2026-10-20) played before NBA polling started.
  - The 7 upcoming games without a line are 2-3 days out, before books post.

### M16 replay on real data (verification c, NBA)

The dev copy of the NBA history was used at 2026-02-11 22:05 UTC: the poller read the real 5:00 PM ET report (`Injury-Report_2026-02-11_05_00PM.pdf`). Evan Mobley's previous row for that game had been set to Questionable, so the report changed him to Out.
- In one cycle the poller loaded the report and found **one** qualifying change: Mobley, 34.1 min per game played, play probability 0.50 → 0.005.
- It re-predicted **only** WAS @ CLE with nba15 (CLE margin 16.5 → 15.5, morning point), in 76 s including the store reload.
- The CI test `tests/alerts/test_poller.py` covers the same chain with stubbed models: one cycle, one re-predicted game, exactly one alert pushed. It also covers de-duplication, quiet hours and Signal 2 re-alerting only after the gap closes.
