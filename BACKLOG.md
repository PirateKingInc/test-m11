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

## Review items (REVIEW.md)
- #4 units `predict` defaults can drift from what the backtest chose.
- #5 injury change-only key ignores team (mid-week trades).
- #6 snap-count name matching can merge two same-named teammates.
- #7 units alpha/revert chosen on in-sample error.
- #13 private report helpers imported across modules.
