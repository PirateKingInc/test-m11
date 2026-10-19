# M12 NFL model: design, as-of rules and results log

M12 is NFL only. Its aim is to build every game-moving variable free data allows, with no
look-ahead, and keep a variable only if it measurably improves prediction on seasons the model
was not fitted on. Elo (M10) and units+missing (M11) stay runnable as baselines
(`betdata backtest nfl --model elo|units`).

## Prediction points

Every game is predicted at two instants (`src/betdata/nfl/points.py`):

| Point | When (US Eastern) | What is new at this point |
|---|---|---|
| `final_report` | 17:00 on the day of the last official injury report: two days before a Sunday or Monday game (Friday, Saturday), one day before a game on any other day (Thursday games use Wednesday, Saturday games use Friday). | Final game statuses (Out, Doubtful, Questionable) |
| `prekick` | kickoff minus 75 minutes | Game-day inactives, the weekly roster, roof open/closed, referee, latest forecast and line |

## As-of rules (when each row becomes known)

Every row used carries a `known_at` timestamp, and a feature at instant `t` reads only rows with
`known_at <= t`. Where a source has no publish time, a conservative rule is used.

| Data | `known_at` | Basis |
|---|---|---|
| Schedule: teams, kickoff, site, week, venue, surface, roof *type*, division | always known | Published before the season |
| Head coach for the game (games.csv) | the game's `final_report` instant | Coaching changes are announced days before |
| Final score, team stats, snap counts, play-by-play aggregates of a game | kickoff + 12 h | Rule: nflverse rebuilds overnight; 12 h covers it |
| Injury report row | `date_modified` of the nflverse row (stored as `report_date`) when the row was backfilled after kickoff; else the time we fetched it | Real publish timestamp |
| Practice participation by day | time we fetched it (daily job) | History only has the last practice status of the week |
| Weekly roster of week N (ACT/RES/INA/CUT...) | the team's week-N `prekick` instant; before that, week N-1's roster | The file is the game-day snapshot (holds INA) |
| Retractable roof open/closed | `prekick` | Decided on game day |
| Referee | `prekick` | Rule: assignments are not officially published in advance |
| Weather forecast (Open-Meteo) | the forecast row's `fetched_at` | Stored forecasts only; observed weather is never an input |
| Market line and total | `fetched_at` of `poll` snapshots | `source_closing` rows and `v_closing_lines` are never inputs |

Never inputs: games.csv `temp`, `wind`, `home_qb_id`/`away_qb_id`, `result`, `total`; observed
weather; closing lines.

The as-of harness (`src/betdata/nfl/asof.py`, `tests/nfl/test_asof_harness.py`) rebuilds every
feature group as of an instant `t` three ways: from all data, from only the rows known at `t`,
and from all data with every row known after `t` scrambled. It fails if any value differs.
`betdata nfl asof-check` runs the same check on the real database for a sample of games.

## Seasons and acceptance rule

* **Train** 2013-2019: coefficients are fitted here only.
* **Validate** 2020-2022: every keep/drop decision and hyper-parameter is chosen here.
* **Test** 2023-2025: scored once, at the end, after refitting on 2013-2022 with the frozen
  design. Nothing is re-tuned afterwards. Note: 2020-2025 were the M11 test seasons and
  2015-2025 the M10 test seasons, so none of these years is pristine (REVIEW.md #8). What M12
  promises is that no M12 choice is made by looking at 2023-2025.
* **Base model**: home field plus the M10 Elo rating difference for the margin, and the
  rolling league scoring level for the total.
* **Groups are added one at a time**, in the order below. A group is **kept** if, added to the
  kept set, it lowers validation mean absolute error (MAE) on the margin or on the total. MAE is
  averaged over the two prediction points. Every result, kept or dropped, is logged below.
* **Stop rule**: per group, one full build plus at most 3 targeted adjustments judged on
  validation data. If it still doesn't help, it is dropped and the reason recorded.
* **Model**: ridge regression (standardised features; separate fits for margin and total). A
  gradient-boosting model on the same features is the challenger; the simpler one wins unless
  the other is clearly better on validation.
* **Win probability**: P(home wins) = Phi(margin / sigma), with sigma fitted on training
  residuals. Calibration is checked on validation (bins, Brier score).
* **Blend**: model + market = w x model + (1 - w) x market line at the prediction point.

### Group 1: player availability (`features/availability.py`)

Full build: valued players vs replacement (QB EPA per dropback, shrunk, scaled to a game; other
positions by snap role), the change against the players of the team's last four games per
position group, QB starter/backup gap and backup inexperience, OL/DB/WR clusters, returning
and newly acquired starters. Play probabilities learned on 2013-2019 (status buckets, e.g. at
the final report Questionable + limited practice 69%, Questionable + no practice 36%, Out 0.3%;
at prekick an active Questionable player 72%, an inactive one 0.3%). Replacement QB: -0.226
EPA per dropback (QBs who were not their team's main passer).

Found while building it: nflverse's 2013-2015 weekly rosters are end-of-season snapshots
(REVIEW.md #19), so roster statuses before 2016 are never used; before 2016 a player's status
comes from the injury report only. Game-day inactives (INA) exist from 2019.

| Adjustment | Margin | Total | Result |
|---|---|---|---|
| 1. Skill players valued by EPA per target/carry above replacement x workload | 9.924 | 10.891 | worse: reverted |

Practice participation by day: nflverse's history keeps only the last practice status of the
week, so the trend can't be learned; live runs store every daily change (`injuries` is
change-only) and the latest status is what the model uses.

### Group 2: team strength (`features/strength.py`)

Full build: opponent-adjusted offense and defense ratings (weighted ridge on every known
team-game, plays x recency weights, half-life 120 days, 250-play prior, 30% shrink between
seasons) for pass EPA, rush EPA, success rate, early-down EPA and explosive-play rate; turnover
luck (fumble recoveries vs 50%, INT rates thrown and caught vs league); pace (seconds per
neutral-situation play) and neutral pass rate for totals; new head coach and new primary QB vs
last season, fading over the first 8 games. The gain over availability + Elo is small
(-0.019 margin, -0.023 total): Elo already carries most of the team-level signal.

| Adjustment | Margin | Total | Result |
|---|---|---|---|
| 1. Half-life 60 days | 9.889 | 10.826 | worse |
| 2. Half-life 240 days | 9.866 | 10.830 | worse |
| 3. Prior 600 plays (more shrinkage) | 9.857 | 10.825 | margin worse, total +0.001: kept the full build |

### Group 3: schedule and travel (`features/schedule.py`)

Full build: rest days (difference, short week <= 5 days, off a bye >= 13), miles from the
team's home stadium, time zones crossed, body clock (kickoff before 11:00 on the team's home
clock), consecutive road games, a sea-level team at altitude (>= 1,000 m), international venue.
All schedule facts, known in advance. Venue time zones and elevations were added to
`seeds/venues_nfl.csv`. Elo already carries the bye-week bonus and home field, so the margin
gain is tiny and the total half didn't help; no adjustment was worth trying beyond the rule.

## Feature groups

Each group lives in `src/betdata/nfl/features/` and passes the as-of harness.

1. **Player availability** (replaces M11's head counts): a value for each player (QB and
   skill players from play-by-play EPA; linemen and defenders from snap share and role), each
   relative to a positional replacement level. Separate QB term: the quality gap between
   starter and backup, and the backup's career starts. Practice trend and game status, with
   play probabilities learned on training seasons. Inactives at `prekick`. Clusters (2+ OL,
   DB or WR out). Players returning from absence, and players new to the team.
2. **Team strength**: opponent-adjusted pass/run offense and defense (recency-weighted), success
   rate, early-down EPA, explosive-play rate, turnover-luck regression (fumble recovery and
   interception rates vs expectation), pace and neutral-situation pass rate, preseason priors
   (prior season, QB change, head coach change) decaying as games are played.
3. **Schedule and travel**: rest difference, short week, post-bye, miles travelled, time zones
   crossed, body clock (West Coast team in an early Eastern kickoff), international game,
   consecutive road games, altitude.
4. **Environment**: forecast wind, temperature and precipitation at the prediction point; roof
   state; playing surface.
5. **Officials**: referee crew penalty and scoring tendencies from that crew's earlier games only.
6. **Coaching**: fourth-down aggressiveness, play-calling tendencies (early-down pass rate over
   expected).
7. **Game context**: divisional game, late-season playoff stakes, likely resting of starters in
   week 18.
8. **Market**: line and total at the prediction point and movement from open to then. Never the
   closing line.

### Data caveats found while loading

* nflverse's 2012 snap-count file is empty, so snap history starts in 2013 (the first
  training season); the first weeks of 2013 have no snap-based roles.
* The 2025 and 2026 injury files have no `date_modified`; their backfilled rows are assumed to
  be the final report (the documented fallback in `injury_known_at`).
* No historical forecasts or prediction-time lines are stored (BACKLOG.md), so the weather
  features and group 8 have no history to learn from.

## Results log

Validation = 2020-2022 (838 games with scores), fitted on 2013-2019; MAE in points, averaged
over the two prediction points. Each group's margin features and total features are judged
separately: the margin half is kept if it lowers margin MAE, the total half if it lowers total
MAE (a group can help one and not the other). The ridge penalty is chosen on validation from
1-3000. Reference: the closing line misses validation games by
**9.747** (margin) and **10.507** (total). `betdata nfl evaluate` reproduces every row.

| Step | Margin MAE | Total MAE | Decision | Notes |
|---|---|---|---|---|
| Base (home field + M10 Elo; league scoring level) | 10.086 | 11.175 | base | ridge penalty 100 / 1 |
| + 1 Player availability | **9.874** | **10.849** | KEEP / KEEP | -0.21 margin, -0.33 total. Prekick (inactives known) beats the final report: 9.843 vs 9.905 margin. |
| + 2 Team strength | 9.855 | 10.826 | KEEP / KEEP | ridge penalty rises to 1000 (margin) / 300 (total) |
| + 3 Schedule and travel | 9.854 | 10.832 | KEEP / drop | margin -0.001 (within noise; kept by the rule); total worse |
