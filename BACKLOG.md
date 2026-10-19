# Backlog

Items deliberately left out of M12, with the reason. Newest first.

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
