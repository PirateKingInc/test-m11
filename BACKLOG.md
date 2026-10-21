# Backlog

Items deliberately left out of M12, with the reason. Newest first.

## M13 additions (hourly page)
- **Fresher free lines.** Without a SharpAPI key, the page's market lines come from nflverse's `games.csv`, which shows one line per game with no named book. It is updated a few times a day, not hourly. A SharpAPI key (DraftKings + FanDuel, free tier) makes hourly lines real. Other options are a free live feed, such as ESPN's public scoreboard odds, or The Odds API on more than its pre-kickoff schedule (the 500 credits can't support hourly: 16 x 3 x 30 = 1,440).
- **Prediction and grade history survives only in the Actions cache.** If the cache is evicted, the next run rebuilds all data from nflverse, but stored predictions, grades and run history from before the loss are gone (the page says so). Back them up as a small JSON or SQLite asset (release asset or data branch) so a rebuild can restore them.
- **Raw archive growth.** Every changed nflverse file is archived gzipped, about 0.5 MB per `games.csv` or `roster_weekly` version. Hourly polling adds a few MB a day to the database and the cached state. Prune superseded raw files older than a season, or move the archive out of the cache.
- **Scheduled workflows stop after 60 days without repository activity** (GitHub's rule for public repos). Merges keep it alive in season. A monthly keep-alive (or re-enabling in the Actions tab) is needed across a quiet off-season.
- **Moneyline calibration:** validation showed the model overrating home teams in 2020-2022 (Platt intercept -0.22; 2020 had no crowds). Per the M13 stop rule, nothing was applied. A home-field term refitted on recent seasons is the candidate fix.
- **Weather does not help totals.** With archived forecasts from 2022 on, adding `wx` to the total model raised validation MAE (10.736 → 10.786), and forecasts match the observed stand-in in scale (bias -0.5 °C, +0.2 km/h). Candidates: a wind-direction term against stadium orientation, and kicking-game splits, i.e. effects on field goals rather than on the total.
- **Open-Meteo per-minute limit:** the backfill hit HTTP 429 once. Add a short back-off on 429s (it is retried by the next weekly run).
- **Actions minutes if the repository goes private:** about 1,980 billed minutes a month with the :30 backup trigger, at the free plan's 2,000. Drop the backup trigger, or check the slot inside the gate job, before going private.
- **Game-day inactives** arrive only when nflverse republishes weekly rosters (unchanged from M12). The hourly runner refreshes them every hour, so prekick picks pick them up within the hour once published.

## Other sports (out of M12 scope)
- NCAAF, NBA, NCAAB models: M12 is NFL only. The ingestion for all three is in place (CFBD,
  nba_cdn, sportsdataverse, SharpAPI, OddsPapi); the NFL as-of framework (`src/betdata/nfl/`)
  is the template.

## Data unavailable from this environment
- **Historical opening / mid-week lines.** nflverse has closing lines only. The free archives
  (aussportsbetting.com xlsx, sportsbookreviewsonline.com) are refused by this machine's
  network proxy (HTTP 403 on CONNECT). The market group therefore can't be validated on
  history; it uses lines the scheduler stores live (nflverse daily poll, The Odds API, SharpAPI).
- **Open-Meteo** (forecast, archive, historical-forecast and previous-runs APIs) is also
  refused by the proxy here. Weather features read stored forecasts only, so there is no
  forecast history to validate them on. From an unblocked machine: run the scheduler's
  `weather-forecast-nfl` job in season. For history, add a backfill from Open-Meteo's
  previous-runs API (forecasts with 1-7 day lead).

## Data available but not used (M12)
- **Coordinator changes**: no free source (nflverse has head coaches only). The preseason prior
  uses head coach and starting QB.
- **Game-day inactives live**: the model uses them at prekick, and history has them (weekly
  rosters, status INA, 2019+). Live, they arrive only when nflverse republishes the weekly
  roster file, which is not guaranteed before kickoff (the scheduler refreshes it in the two
  hours before each window). A faster inactives feed would make live prekick picks match the
  backtest.
- **Practice participation by day**: nflverse history keeps only the last practice status of
  each week, so the trend can't be learned; the daily change-only rows are being stored from
  now on.
- **Participation (pbp_participation, 2016+), FTN charting (2022+), Next Gen Stats, PFR
  advanced stats, depth charts**: checked, not used. Player values come from play-by-play EPA
  and snap counts. Defender-level on-field EPA from participation is very noisy, and depth
  charts have no reliable publish time before 2025. Candidates for a later group.
- **Weekly rosters before 2016** are end-of-season snapshots (REVIEW.md #19). Players' statuses
  for 2013-2015 come from injury reports only.
- **2012 snap counts**: nflverse's file is empty.
- **Game context** (division game, playoff race, week-18 resting) was built and dropped (no
  validation gain); a proper playoff-odds simulation could replace the record-based proxies.

## Review items (REVIEW.md)
- #4 units `predict` defaults can drift from what the backtest chose.
- #5 injury change-only key ignores team (mid-week trades).
- #6 snap-count name matching can merge two same-named teammates.
- #7 units alpha/revert chosen on in-sample error.
- #13 private report helpers imported across modules.

## M14 (NBA) additions
- **Historical lines before tip, 2017-18 to 2022-23.** ESPN keeps only the closing line for these seasons, so the market group (line at the point, movement since the open) could not be validated and is not in the model. Lines are now stored hourly from ESPN's DraftKings feed; after a season of them, re-run group 7 on the new data.
- **Injury reports before Dec 2018.** The league's PDFs before 2018-12-17 answer 403. Availability in 2015-18 rests on box-score absences only.
- **Every report version.** History loads the hourly reports 11 AM-10 PM ET. The 15-minute versions (2026) and overnight reports exist but were not loaded (~20,000 more requests at 1 a second).
- **Opening-night rosters.** Until a team's first game of a season is over, its rotation is last season's: offseason signings and trades are invisible, and departed players still count. A preseason roster feed (ESPN team rosters) would fix that.
- **In-Season Tournament games** are not flagged: the stored schedule doesn't mark them. sportsdataverse's `notes_headline` does.
- **Coaching changes:** no free source wired in; not modelled.
- **A gradient-boosting challenger** for NBA, as M12 did for NFL.
- **The "Removed" status** (a player dropped from a later report) was seen only 35 times in training, so its play probability (5%) is not trustworthy. Map it to "not on the report" if it stays rare.
- **Closing totals** are in the store but the totals model stays in the CLI (as NFL).
