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

### Group 4: environment (`features/environment.py`)

Full build: dome / retractable roof (open or closed known only at prekick, half indoor before
that), artificial turf, and forecast wind above 10 km/h, cold below 5 C and rain from the
latest Open-Meteo forecast fetched by the prediction point. The forecast part has nothing to
learn from: no historical forecasts are stored, and Open-Meteo (including its historical-forecast
and previous-runs APIs) is refused by this machine's proxy (BACKLOG.md). Every weather feature
is zero in 2013-2022, so its fitted coefficient is zero. The model ignores weather until
forecasts have accumulated and the group is refitted. Observed weather (nflverse temp/wind,
Open-Meteo archive) is never used. Roof and surface alone cut total error by 0.09 points.

### Group 5: officials (`features/officials.py`)

Full build: the referee (crew chief) from games.csv, known at prekick (assignments aren't
officially published in advance), so at the final report every crew counts as average. A crew's
penalties per game, points per game and home-minus-away penalty split come from its earlier
games over three seasons only, shrunk with 16 games of league average. Only the margin half
survives (by 0.002, within noise): crew effects on totals didn't hold up on 2020-2022.

### Group 6: coaching (`features/coaching.py`)

Full build: for the head coach named for the game (known by the final report), from every
earlier game he coached for any team: fourth-down go rate in 4th-and-1-to-3 between the 40s
(win probability 5-95%), shrunk with 15 chances of league average; early-down neutral-situation
pass rate over expected (nflfastR `pass_oe`), shrunk with 150 plays. A clear margin gain
(-0.031): aggressive, pass-first coaches win by more than their ratings say. No help for totals.

### Group 7: game context (`features/context.py`), dropped

Full build: division game (home-field interaction for the margin), and from week 12, from
the regular-season record known at the point: in the playoff race (.400-.650), out of it
(< .350), and in the last regular-season week a likely-clinched team (>= .750) that may rest
starters. Worse on both targets.

| Adjustment | Margin | Total | Result |
|---|---|---|---|
| (full build) | 9.845 | 10.763 | worse than 9.821 / 10.734 without it |
| 1. Division game and week-18 resting only | 9.846 | 10.781 | worse |
| 2. Division game only | 9.834 | 10.761 | worse: dropped |

Why: division games and late-season stakes are already priced through team strength and
availability (resting starters show up as inactives at prekick). Record-based stakes are a
crude proxy for playoff odds (BACKLOG.md).

### Group 8: market at the prediction point (`features/market.py`)

Full build: the latest polled spread and total fetched by the prediction point (nflverse's
daily poll, The Odds API, SharpAPI, OddsPapi; never a `source_closing` row or
`v_closing_lines`), and the move since the first line we stored. History has no such lines:
nflverse keeps closing lines only, and the free opening-line archives are refused by this
machine's proxy (BACKLOG.md). Every market feature is zero in 2013-2022, so the group has
nothing to learn and is not in the fitted model (validation unchanged, 9.821 / 10.734).

The market enters live predictions as a **blend** instead, w x model + (1 - w) x the line at
the prediction point, with w chosen on validation (Model selection below). `betdata predict
nfl` prints and stores both the model-only number and the blend.

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
| + 4 Environment | 9.854 | **10.734** | - / KEEP | total -0.092: roof and surface. No forecast history, so weather can't be fitted yet |
| + 5 Officials | 9.852 | 10.746 | KEEP / drop | margin -0.002 (within noise); totals worse |
| + 6 Coaching | **9.821** | 10.757 | KEEP / drop | margin -0.031; totals worse |
| + 7 Game context | 9.845 | 10.763 | drop / drop | worse on both after 2 adjustments |
| + 8 Market | 9.821 | 10.734 | drop / drop | no prediction-time lines in history: nothing to fit. Used live as a blend |
## Model selection (validation 2020-2022, `betdata nfl select`)

Kept design (`config/nfl_model.json`): margin = base + availability + strength + schedule +
officials + coaching (ridge penalty 1000); total = base + availability + strength +
environment (penalty 300).

| Prediction point | Ridge margin / total | Gradient boosting margin / total |
|---|---|---|
| final report | 9.850 / 10.745 | 10.125 / 10.851 |
| prekick | 9.792 / 10.723 | 10.167 / 10.888 |

Ridge wins both by a wide margin (boosting with absolute-error loss, 300 trees, 15 leaves,
40 games per leaf), so the simpler model is kept. `scikit-learn` is a dependency only for this
comparison.

**Win probability**: P(home wins) = Phi(margin / 13.1), sigma from training residuals.
Validation Brier score 0.217 (final report) / 0.216 (prekick), log loss 0.623 / 0.620 (834
games without ties). Reliability bins, prekick:

| Predicted | Games | Mean predicted | Home won |
|---|---|---|---|
| 0.1-0.2 | 6 | 0.168 | 0.333 |
| 0.2-0.3 | 33 | 0.254 | 0.303 |
| 0.3-0.4 | 91 | 0.354 | 0.231 |
| 0.4-0.5 | 154 | 0.453 | 0.383 |
| 0.5-0.6 | 193 | 0.551 | 0.508 |
| 0.6-0.7 | 188 | 0.644 | 0.590 |
| 0.7-0.8 | 130 | 0.741 | 0.792 |
| 0.8-0.9 | 35 | 0.834 | 0.971 |
| 0.9-1.0 | 4 | 0.918 | 0.750 |

Roughly calibrated. The middle bins run 4-12 points high, the 0.7-0.9 bins low: home teams won
a little less often than the model expected in close games during 2020-2022 (2020 had no
crowds, which the training seasons can't know about).

### Ablation

Each group added in turn is the results log above. Each group removed from the final model
(validation MAE, margin / total):

| | Margin | Total |
|---|---|---|
| all kept | 9.821 | 10.734 |
| without availability | 10.009 (+0.188) | 10.811 (+0.077) |
| without strength | 9.862 (+0.041) | 10.772 (+0.038) |
| without schedule | 9.824 (+0.003) | (not in total model) |
| without officials | 9.823 (+0.002) | (not in total model) |
| without coaching | 9.852 (+0.031) | (not in total model) |
| without environment | (not in margin model) | 10.826 (+0.092) |

Player availability is by far the most important group. Schedule and officials are within
noise.

### Model + market blend

History has no lines from before kickoff, so the blend weight is chosen on validation with
the closing line standing in for the prediction-time line. This picks one number per market
and is never a model input. Validation MAE, prekick:

| | Model alone | Market (close) alone | Best blend |
|---|---|---|---|
| Margin | 9.792 | 9.747 | **9.706** at 0.40 model / 0.60 market |
| Total | 10.723 | 10.507 | **10.490** at 0.15 model / 0.85 market |

The model adds information the closing line doesn't fully have on the margin (the blend beats
both), and very little on totals. Live, the blend uses the line known at the prediction point,
which is less informed than the close, so the true live benefit of blending is probably larger
than this measure suggests.

## Final test: 2023-2025, scored once (`betdata nfl final-test`)

The frozen design was refitted on 2013-2022 and scored on the 855 test games with M12, Elo,
M11 and both closing lines (2026-10-19, `data/reports/m12_final_test.json`). Nothing was
changed afterwards. Elo uses M10's settings; M11 is units+missing fitted on 2013-2019 (as
M11 was), with the REVIEW.md fixes.

| Miss on the final margin | final report | prekick |
|---|---|---|
| M12 | 10.062 | **9.996** |
| Elo (M10) | 10.259 | 10.259 |
| M11 units+missing | 10.314 | 10.314 |
| Closing line | 9.786 | 9.786 |

| Miss on total points | final report | prekick |
|---|---|---|
| M12 | 10.379 | **10.375** |
| M11 units+missing | 10.459 | 10.459 |
| Closing total | 10.120 | 10.120 |

Win probability on test: Brier 0.218 (final report) / 0.217 (prekick), log loss 0.626 / 0.623,
close to validation. Reliability (prekick): predicted 0.26 → won 0.29, 0.36 → 0.31,
0.45 → 0.36, 0.55 → 0.61, 0.65 → 0.65, 0.75 → 0.76, 0.84 → 0.82.

Bets against the closing line at the existing thresholds (3 pts spread, 4 pts total), -110:

| | Record | Win % | 95% range | ROI |
|---|---|---|---|---|
| M12 spread, prekick | 107-88-3 | 54.9% | 47.9-61.7% | +4.7% |
| M12 spread, final report | 113-98-4 | 53.6% | 46.8-60.2% | +2.2% |
| M12 total, prekick | 58-66-1 | 46.8% | 38.2-55.5% | -10.6% |
| M12 total, final report | 81-70-2 | 53.6% | 45.7-61.4% | +2.4% |
| Elo spread | 135-138-4 | 49.5% | 43.6-55.3% | -5.5% |
| M11 spread | 127-138-7 | 47.9% | 42.0-53.9% | -8.3% |
| M11 total | 109-95-2 | 53.4% | 46.6-60.1% | +2.0% |

**Reading**: M12 is the most accurate model so far: 0.26 points of margin error better than
Elo and 0.32 better than M11, with totals 0.08 better than M11. It is still 0.21 (margin) and
0.26 (total) points *worse* than the closing line. Every betting record's 95% range includes
the 52.4% break-even. The spread records lean positive, but 200 bets can't tell that from luck.
These bets are graded at the close, where the market has already absorbed the news the model
uses. The live test is whether a prediction-time bet beats the line it is placed at, and that
is what `betdata grade nfl` now measures (CLV).
