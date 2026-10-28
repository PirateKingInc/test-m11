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

| M13 weather group `wx`: Open-Meteo previous-runs forecast | the prediction point | The run is `lead_days` before the game's hours, chosen so it was out (6 h latency) before the point |
| M13 `wx`: Open-Meteo historical-forecast archive | the prediction point | Fallback only: short lead, may have been issued after the point (flagged by its source) |
| M13 `wx`: nflverse game-day `temp`/`wind`, 1999-2021 | the prediction point, **by convention** | Observed weather standing in for a forecast where none was archived (M13 spec). A declared exception: training sees the weather that happened, so it overstates how well a forecast predicts it |

Never inputs to M12: games.csv `temp`, `wind`, `home_qb_id`/`away_qb_id`, `result`, `total`;
observed weather; closing lines. M13's weather version uses `temp`/`wind` only as the stand-in
above.

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
## Group details

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

## M13 Part A: two open questions from M12

### 1. Final test (2023-2025) at the final_report point

Read from the frozen M12 run (`data/reports/m12_final_test.json`, 2013-2022 fit, 855 test
games). Nothing was refitted. The tables are laid out like the prekick results so the two
points can be compared row by row.

| Miss on the final margin | final report | prekick |
|---|---|---|
| M12 | 10.062 | 9.996 |
| Elo (M10) | 10.259 | 10.259 |
| M11 units+missing | 10.314 | 10.314 |
| Closing line | 9.786 | 9.786 |

| Miss on total points | final report | prekick |
|---|---|---|
| M12 | 10.379 | 10.375 |
| M11 units+missing | 10.459 | 10.459 |
| Closing total | 10.120 | 10.120 |

| Win probability | final report | prekick |
|---|---|---|
| Brier | 0.2179 | 0.2166 |
| Log loss | 0.626 | 0.623 |
| Games | 854 | 854 |

Reliability, shown as predicted → won (games):

| Bin | final report | prekick |
|---|---|---|
| 0.1-0.2 | 0.17 → 0.33 (12) | 0.16 → 0.33 (9) |
| 0.2-0.3 | 0.25 → 0.29 (41) | 0.26 → 0.29 (48) |
| 0.3-0.4 | 0.35 → 0.30 (105) | 0.36 → 0.31 (106) |
| 0.4-0.5 | 0.45 → 0.40 (179) | 0.45 → 0.36 (171) |
| 0.5-0.6 | 0.55 → 0.60 (163) | 0.55 → 0.61 (157) |
| 0.6-0.7 | 0.65 → 0.64 (167) | 0.65 → 0.65 (177) |
| 0.7-0.8 | 0.74 → 0.78 (118) | 0.75 → 0.76 (120) |
| 0.8-0.9 | 0.84 → 0.76 (63) | 0.84 → 0.82 (61) |
| 0.9-1.0 | 0.92 → 1.00 (6) | 0.93 → 1.00 (5) |

Bets against the closing line at the existing thresholds (3 pts spread, 4 pts total), -110:

| | final report | prekick |
|---|---|---|
| M12 spread | 113-98-4, 53.6% (46.8-60.2%), ROI +2.2% | 107-88-3, 54.9% (47.9-61.7%), ROI +4.7% |
| M12 total | 81-70-2, 53.6% (45.7-61.4%), ROI +2.4% | 58-66-1, 46.8% (38.2-55.5%), ROI -10.6% |

The final_report point is 0.07 points worse on margin than prekick and about the same on
totals, which is what the 1-2 days of extra news between the two points would suggest. Both
points' bet records have 95% ranges that include the 52.4% break-even.

### 2. Why M11's spread bet count went from 293 to 1,023

**No threshold or bet rule changed in code.** M11's rule (`analysis/backtest.choose_threshold`)
picks the spread threshold with the best *training* ROI among 1, 1.5, 2, 2.5, 3, 4 and 5 points,
counting only thresholds that bet at least 200 times on training. The rule ran again after each
leak fix and chose a different threshold each time, because removing the leaks moved
training ROI:

| M11 units+missing, test 2020-2025 | threshold (chosen on train) | spread bets | record | ROI |
|---|---|---|---|---|
| Before the fixes (leaky) | 4 pts | **293** | 155-137-1 (53.1%) | +2.2% |
| After REVIEW #1-#3 | 2 pts | 821 | 405-403-13 (50.1%) | -3.3% |
| After #19 too (current) | 1.5 pts | **1,023** | 495-511-17 (49.2%) | -4.9% |
| Current, threshold held at 4 pts | 4 pts (fixed) | 304 | 156-145-3 (51.8%) | -0.1% |

With the leaks in place, the 4-point threshold looked best on training. With them removed,
it loses on training (-3.3% ROI over 307 bets), and the rule moves down to the threshold that
loses least. The jump in bet count is therefore the documented rule doing what it was built
to do; it was not an accidental change. Held at the original 4 points, the corrected M11
makes about the same number of bets (304 vs 293) and breaks even (-0.1%) instead of winning
(+2.2%). Either way the conclusion holds: once the leaks are gone, M11 has no spread edge.

I kept the rule as it is. Picking a threshold after looking at test results would be tuning on
the test seasons. The table in REVIEW.md now shows both numbers.

While checking this, I found and fixed a bug: `betdata backtest nfl --model units --threshold
X` silently ignored `--threshold` and always used the threshold chosen on training. It now
fixes the spread threshold, which is how the "held at 4 pts" row above was produced.

## M13 moneyline (`betdata nfl moneyline`, `betdata nfl moneyline-final-test`)

**How the moneyline is built.**
- **Win probability:** the frozen M12 fit gives Φ(margin / σ), with σ the training residual standard deviation of the margin, about 13.1 points.
- **Fair moneyline:** that probability converted to American odds with no margin. For example, 60% → -150 and 40% → +150. Code is in `betdata/moneyline.py`.
- **Market probability:** nflverse's moneyline with the vig removed proportionally, so the two sides sum to 100%.
- **Edge:** the model's probability minus the no-vig market's, in percentage points.

The win probability uses the model margin, not the model+market spread blend, so the edge measures the model's own view.

### Calibration on validation (2020-2022)

The fit is the frozen design on 2013-2019 and 834 decided games, scored against the closing no-vig moneyline.

| Brier (lower is better) | final report | prekick |
|---|---|---|
| M12 model | 0.2172 | 0.2156 |
| No-vig closing market | 0.2098 | 0.2098 |

Reliability, shown as model predicted → home team won:

| Bin | n (fr / pk) | final report | prekick |
|---|---|---|---|
| 0.1-0.2 | 7 / 6 | 0.18 → 0.43 | 0.17 → 0.33 |
| 0.2-0.3 | 29 / 33 | 0.26 → 0.28 | 0.25 → 0.30 |
| 0.3-0.4 | 88 / 91 | 0.35 → 0.23 | 0.35 → 0.23 |
| 0.4-0.5 | 142 / 154 | 0.45 → 0.42 | 0.45 → 0.38 |
| 0.5-0.6 | 222 / 193 | 0.55 → 0.49 | 0.55 → 0.51 |
| 0.6-0.7 | 184 / 188 | 0.65 → 0.59 | 0.64 → 0.59 |
| 0.7-0.8 | 127 / 130 | 0.74 → 0.80 | 0.74 → 0.79 |
| 0.8-0.9 | 30 / 35 | 0.84 → 0.97 | 0.83 → 0.97 |

The 0.0-0.1 and 0.9-1.0 bins have 1-4 games each and are left out.

**One calibration fit.**
- I fitted a Platt fit (a logistic regression of the outcome on logit(p)) once, on the validation predictions with both points pooled. It gives **slope 1.31, intercept -0.22**.
- A slope above 1 means the model is **not overconfident**. Its extreme bins are, if anything, too timid: 0.84 → 0.97.
- The bins from 0.35 to 0.65 run 3-10 points *high* for the home team. That is the negative intercept: 2020 had no crowds and validation-era home field was weaker than in 2013-2019.
- The rule was "calibrate only if overconfident", so **no calibration is applied**. The fit is recorded in `config/nfl_moneyline.json` with `applied: false`.
- Per the stop rule, I did not try anything else, such as a home-field-only correction.

**Bet threshold**, chosen on validation:
- Back the side whose model probability beats the no-vig closing probability by at least the threshold, at the closing price.
- Both points are pooled, and a threshold needs at least 100 bets to be eligible.
- The rule is the same shape as M11's: the best ROI among the eligible thresholds.

| Edge ≥ | Record | Win % | ROI |
|---|---|---|---|
| 2 pts | 527-823-7 | 39.0% | -1.7% |
| **3 pts** | **468-732-6** | **39.0%** | **-0.0%** |
| 4 pts | 405-657-5 | 38.1% | -1.3% |
| 5 pts | 331-564-4 | 37.0% | -3.9% |
| 6 pts | 272-477-3 | 36.3% | -3.1% |
| 8 pts | 176-339-2 | 34.2% | -0.7% |
| 10 pts | 110-240-2 | 31.4% | -8.9% |

- The chosen threshold is **3 points**.
- Every threshold loses or breaks even on validation, so this is the least-bad option, not a found edge.
- The model mostly backs underdogs: the average price at 3 points is +164.

### Final test (2023-2025), scored once

The frozen design is refitted on 2013-2022 with the validation threshold and no calibration. The report is `data/reports/m13_moneyline_final_test.json`.

| | final report | prekick |
|---|---|---|
| Games (decided, with a closing moneyline) | 854 | 854 |
| Brier, M12 | 0.2179 | 0.2166 |
| Brier, no-vig closing market | 0.2102 | 0.2102 |
| Bets ≥ 3 pts at the closing price | 233-336-0 (40.9%), ROI **-8.9%** | 224-334-1 (40.1%), ROI **-9.4%** |

Both points pooled: 457-670-1, ROI -9.2%, with a 95% win-rate range of 37.7-43.4%.

**Reading.**
- The market's win probabilities beat the model's on every split: 0.007-0.008 Brier on both validation and test.
- Moneyline bets on the model's edge lose about 9% at the close on the test seasons.
- The page still shows the model's fair moneyline and edge, and flags games past the 3-point threshold as *paper* picks. The flag is not evidence of an edge.
- `betdata grade nfl` tracks those picks live, including CLV.

## M13 weather (`betdata nfl weather`, `betdata nfl weather-final-test`; workflow `weather`)

M12 had no weather history, so its forecast features were zero in every fitted season. M13 builds that history and tests a weather group, `wx` (`nfl/features/weather.py`), as a **new model version (`m13w`) beside the frozen M12 version**.

**Data.** Table `nfl_weather_asof` has one row per game, prediction point and source. Each row covers the kickoff hour plus the next three. Rows loaded on 2026-10-19 (both points; see the as-of table above for each source's `known_at`):

| Seasons | Source | Rows |
|---|---|---|
| 1999-2021 (2010-2021 used) | nflverse game-day `temp`/`wind` (observed; the stand-in) | 352-402 a season |
| 2022-2026 | Open-Meteo previous-runs forecast, issued before the point (1 day ahead for prekick, 2-3 for the final report) | 396-470 a season |
| 2022-2026 | Open-Meteo historical-forecast archive (short-lead fallback) | 448-470 a season |

The two Open-Meteo APIs answered from GitHub's runners; this development machine cannot reach them. One request hit Open-Meteo's per-minute limit (HTTP 429), so a few 2023-2024 games have only the fallback row.

**Features** (outdoor share as in group 4; zero indoors):
- wind above 10 and above 25 km/h;
- cold below 5 °C;
- heat above 27 °C;
- rain.

The margin model gets wind and cold. There is no "weather known" flag: in a local check it only encoded which seasons had data, and it biased every outdoor total in a season without coverage.

**Validation** (fit 2013-2019, score 2020-2022, M12's penalties kept, `wx` added to M12's design):

| Mean of both points | M12 design | + `wx` | Kept? |
|---|---|---|---|
| Margin MAE | 9.820 | 9.819 | yes (-0.001) |
| Total MAE | 10.736 | 10.786 | **no** (+0.050) |

By M12's rule, a group joins a model only where it lowers validation error. The weather version is therefore **M12 + `wx` in the margin model only** (`config/nfl_model_wx.json`). Its gain is negligible.

Weather made totals *worse*. One possibility was a scale gap between the observed stand-in, which the model learns from in 2013-2019, and the forecasts used from 2022 onward. The diagnostic in `betdata nfl weather` rules that out. On 421 games from 2022 onward that have both, the prekick forecast minus nflverse's reported game-day weather averages -0.5 °C and +0.2 km/h, with mean absolute differences of 1.8 °C and 3.7 km/h. So the stand-in and the forecasts are on the same scale.

The plain reading is that, given everything else the model knows, weather adds no usable signal for totals. Per the stop rule, I tried nothing further.

### Final test of the weather version (2023-2025, scored once)

Both designs were refitted on 2013-2022 and scored on the same 855 games. Run: GitHub Actions `weather` #2; report `data/reports/m13_weather_final_test.json` in the runner state.

| Margin MAE | M12 | m13w (weather) |
|---|---|---|
| final report | 10.061 | **10.057** |
| prekick | 9.996 | **9.992** |

- **Totals** are identical because `wx` is not in the total model: MAE 10.377 at the final report and 10.374 at prekick, and 10.115 / 10.093 on the 467 outdoor games with weather.
- **Totals bets** are therefore the same for both versions: 81-67-2 at the final report and 57-66-1 at prekick.

**Reading.** The weather version is 0.004 points better on margin, which is noise-level. It is shipped as a separate version per the M13 spec, and the page shows it because validation kept it. M12 stays frozen beside it. Both are predicted, stored and graded every hour, and the season record lists them side by side.

## Live-week replay (`betdata nfl replay`)

What M12 (the live fit, 2013-2025) would have said at each prediction point for the games in
`live/*.csv` (2026 weeks 6-7), next to what M10 (Elo) and M11 (units) logged and what happened.
Spreads are the home side; "result" is minus the home margin, on the same scale. Markets are
the lines logged with the live picks: no line history for those days is stored.

```
kickoff (UTC)    game           M10    M11  M12 fr  M12 pk    mkt  result | M11 tot M12 tot mkt tot actual
2026-10-18 13:30 HOU@JAX       -4.1      -    -2.4    -2.6   -2.5    +3.0 |       -    42.3       -   65.0
2026-10-18 17:00 CHI@ATL       -0.4   -3.2    -2.7    -2.3   +1.5    +8.0 |    49.2    48.2    52.5   54.0
2026-10-18 17:00 BAL@CLE       +1.8   +1.9    +2.4    +1.2   +3.0    -3.0 |    42.0    42.8    39.5   23.0
2026-10-18 17:00 TEN@IND       -8.1   -5.4    -7.6    -6.1   -6.5    +6.0 |    46.3    42.0    44.5   46.0
2026-10-18 17:00 NYJ@NE        -9.5   -9.5    -8.6    -7.4   -7.5    -7.0 |    44.9    42.5    43.5   45.0
2026-10-18 17:00 NO@NYG        -5.2   -1.8    -1.8    -1.2   +1.5    +7.0 |    45.8    42.7    46.5   61.0
2026-10-18 17:00 CAR@PHI       -2.2   -1.2    +3.3    +4.3   +3.0   +11.0 |    44.4    41.6    39.5   39.0
2026-10-18 17:00 PIT@TB        +0.6   -0.4    -0.2    +0.4   +2.5    +7.0 |    43.6    42.2    38.5   45.0
2026-10-18 20:05 ARI@LA        -9.1  -12.6   -10.9   -11.9  -12.5    +2.0 |    49.8    48.1    51.5   34.0
2026-10-18 20:25 LAC@KC        -9.4   -8.4   -10.1   -10.1   -7.5   -16.0 |    40.3    42.5    41.5   28.0
2026-10-18 20:25 BUF@LV        +4.0   +5.7    +7.1    +6.8   +5.5    +3.0 |    48.9    52.0    53.5   63.0
2026-10-19 00:20 DAL@GB        -4.8   -5.4    -5.1    -4.7   -1.5       - |    53.5    50.0    53.5      -
2026-10-20 00:15 WAS@SF       -10.9   -7.2   -11.7   -11.9   -7.5       - |    49.6    45.4    52.5      -
2026-10-23 00:15 NE@CHI        +0.1      -    -0.2    -0.6   -1.5       - |       -    47.0       -      -
Spreads are the home side (negative = home favoured); result = minus the home margin, on the same scale. mkt = the line logged with the live picks.
  HOU@JAX out at prekick: home: OL Anton Harrison (0%); away: OL Braden Smith (0%), S Jalen Pitre (0%), WR Tank Dell (0%)
  CHI@ATL out at prekick: home: CB A.J. Terrell (0%), CB C.J. Henderson (1%); away: DL Montez Sweat (0%), LB T.J. Edwards (1%)
  BAL@CLE out at prekick: home: OL Teven Jenkins (1%); away: OL Cam Jurgens (1%), OL Ethan Pocic (0%), OL Jovaughn Gwyn (0%), QB Lamar Jackson (1%)
  TEN@IND out at prekick: home: DL Arden Key (0%), LB Jaylon Carlies (1%), WR Alec Pierce (0%), WR Darius Slayton (0%); away: OL Fernando Carmona (0%), OL Jackson Slater (0%), S Kevin Winston Jr. (1%)
  NYJ@NE out at prekick: home: CB Carlton Davis III (1%), OL Mike Onwenu (0%), WR Mack Hollins (0%); away: OL Dylan Parham (1%), WR Adonai Mitchell (1%)
  NO@NYG out at prekick: home: CB Colton Hood (1%), LB Brian Burns (0%), QB Jaxson Dart (0%), S Tyler Nubin (1%); away: LB Kaden Elliss (1%), OL Kelvin Banks Jr. (0%)
  CAR@PHI out at prekick: home: LB Jalyx Hunt (1%), OL Cam Jurgens (6%), OL Landon Dickerson (0%), QB Jalen Hurts (0%); away: CB Jaycee Horn (0%), CB Mike Jackson (0%), OL Monroe Freeling (5%)
  PIT@TB out at prekick: home: CB Benjamin Morrison (5%), QB Baker Mayfield (1%), S Antoine Winfield Jr. (1%); away: S DeShon Elliott (0%), WR Michael Pittman (0%)
  ARI@LA out at prekick: home: OL Alaric Jackson (0%); away: CB Will Johnson (0%), OL Isaiah Adams (0%), OL Paris Johnson Jr. (0%), S Andrew Wingard (1%), WR Marvin Harrison Jr. (6%)
  LAC@KC out at prekick: home: OL Josh Simmons (1%), WR Rashee Rice (1%); away: OL Joe Alt (1%), OL Kayode Awosika (1%), OL Rashawn Slater (0%), S Derwin James (1%), WR Tre Harris (1%)
  DAL@GB out at prekick: home: LB Edgerrin Cooper (0%), OL Zach Bako-Bewele (0%); away: CB Cobie Durant (1%), S Jalen Thompson (0%)
  WAS@SF out at prekick: home: LB Dre Greenlaw (0%), S Marques Sigle (1%), WR Deebo Samuel Sr. (1%); away: CB Trey Amos (0%)
  NE@CHI out at prekick: home: DL Montez Sweat (2%); away: OL Mike Onwenu (2%), WR Mack Hollins (2%)
```

Over the 11 finished games, mean miss on the margin: M12 prekick **7.06**, M12 final report
7.57, Elo 7.95, M11 8.33 (10 games), logged market line 6.77. On totals M12 missed by 10.74,
M11 8.84 and the market 8.30 (10 games). Eleven games prove nothing either way. The spread
gains come from the information M10/M11 lacked: starting QBs out at prekick (Hurts, Lamar
Jackson, Mayfield, Dart). CAR@PHI is the clearest case: M12 PHI +4.3 vs M10 -2.2 / M11 -1.2;
PHI lost by 11.

## Sample `betdata predict nfl` (2026-10-19)

```
m12:22460361b5: model line and total next to the market (blend = model and market combined); factors in points, largest first
Tue 10-20 00:15Z  WAS @ SF   [final_report]  line: model SF -11.2 (blend SF -9.0), market SF -7.5  |  total: model 45.3 (blend 51.4), market 52.5  |  SF win 81%
    line factors (points for SF): QB quality +3.4, Elo ratings +2.2, success rate +1.4, pass offense/defense +1.2
    total factors: success rate +1.2, explosive plays -0.9, pass offense/defense +0.7, roof -0.6
    SF likely out: S Marques Sigle (0%), WR Deebo Samuel Sr. (0%)
Fri 10-23 00:15Z   NE @ CHI  [early]  line: model NE -1.2 (blend CHI -1.0), market CHI -2.5  |  total: model 46.4 (blend 49.0), market 49.5  |  CHI win 46%
    line factors (points for CHI): pass offense/defense -0.8, DL availability -0.7, Elo ratings -0.7, early-down EPA -0.6
    total factors: explosive plays +1.8, QB quality +0.8, roof -0.6, INTs thrown -0.5
    NE likely out: OL Mike Onwenu (2%), WR Mack Hollins (2%)
    CHI likely out: DL Montez Sweat (2%)
Sun 10-25 13:30Z  PIT @ NO   [early]  line: model PIT -0.9 (blend PIT -1.9), market PIT -2.5  |  total: model 45.9 (blend 44.7), market 44.5  |  NO win 47%
    line factors (points for NO): international game -1.5, Elo ratings -1.2, CB availability +1.1, LB availability +0.9
    total factors: roof +2.2, explosive plays -1.1, inexperienced backup QB +0.5, LB availability -0.5
    PIT likely out: WR Michael Pittman (2%)
    NO likely out: OL Kelvin Banks Jr. (2%)
```

## Verification (Step 3)

| Check | Result |
|---|---|
| a) Full regression | 266 tests pass on SQLite and on Postgres 16 (locally and in CI on every PR). Elo reproduces exactly (10.18 vs close 9.81, 990 bets 492-474-24). M11 changes only through the documented REVIEW.md fixes (10.09 → 10.15 margin). |
| b) As-of harness, every group | Synthetic league: every group passes at both points (`tests/nfl/test_asof_harness.py`, plus a deliberate leak that it catches). Real database: `betdata nfl asof-check --sample 16` over all eight groups: 0 violations in 16 games × 2 points (truncated and scrambled copies). |
| c) Later results never change a prediction | `tests/nfl/test_lookahead.py`: rewriting every result known after a prediction point leaves that prediction unchanged; rewriting earlier ones moves it. |
| d) Ablation | Results log (each group added in turn) and Model selection (each removed from the final model). |
| e) Final test | Above: 2023-2025 scored once. |
| f) Live-week replay | Above. |
| g) Grading fixtures | `tests/nfl/test_grade.py`: win, loss, push, positive and negative CLV, no closing line, postponed, idempotent re-runs, score corrections. |
| Fresh clone | `uv sync && uv run betdata db init && uv run pytest` in a clean directory. |

# M14 NBA model: design, as-of rules and results log

M14 adds an NBA spread and moneyline model to the hourly page under the M12 rules: no
look-ahead, every decision on validation seasons, test seasons scored once. Code lives in
`src/betdata/nba/` (data, points, features, pipeline) and `src/betdata/sports/nba.py` (the
plugin). Seasons are labelled by the year they start (2026 = 2026-27).

## Data sources (all free; each checked from a GitHub Actions runner on 2026-10-20)

| Data | Source | Reachable from Actions | History used |
|---|---|---|---|
| Schedule, tip times, final scores | sportsdataverse ESPN schedule files (`espn_nba_schedules`, GitHub releases); live: ESPN scoreboard API (`site.api.espn.com`) | yes / yes | 2015-16 on |
| Player box scores: minutes, points, plus/minus, starter, did-not-play | sportsdataverse `espn_nba_player_boxscores` (one parquet per season); the current season, which has no file yet: ESPN game summary API, per finished game | yes / yes | 2015-16 on |
| Official NBA injury report (Out, Doubtful, Questionable, Probable, Available) | the league's PDFs on `ak-static.cms.nba.com/referee/injury/` | yes, at about one request a second (a burst of ~2,000 was refused) | see "Injury reports" below |
| Market lines (current) | ESPN scoreboard (DraftKings line, free); SharpAPI DK/FD, The Odds API, OddsPapi when their keys are set | yes | from M14 on |
| Market lines (history: opening and closing) | ESPN core odds API, one call per game (several books) | yes | see "Historical lines" below |
| stats.nba.com, cdn.nba.com | league APIs | **no**: cdn.nba.com answers 403, stats.nba.com does not answer | not used |

### Injury reports

Published report times, sampled on 12 dates (`probe` workflow, 2026-10-20):

| Era | Reports per day | File name | Stated time |
|---|---|---|---|
| 2018-19 to 2020-21 | 3: 1:30, 5:30, 8:30 PM ET (bubble: 11:30 AM, 2:30, 5:30 PM) | `Injury-Report_2019-01-10_05PM.pdf` | 30 minutes after the hour in the name |
| 2021-22 to mid 2025-26 | hourly | `_05PM` | same |
| since early 2026 | every 15 minutes | `_05_15PM` | the time in the name |

**Loaded history** (`nba-history` workflow, 2026-10-20/21): the first report reachable is
**2018-12-17**. Nothing earlier answers (2018-01 and older: 403), so 2015-16 to 2017-18 and the
first two months of 2018-19 have no injury report. 139,673 change-only rows. Every row is
matched to a team; 3,995 (2.9%) to no game (postponed or rescheduled games).

History loads the hourly reports 11 AM-10 PM ET on game days (three a day before 2021-22),
which is what the two prediction points read. The other hourly and 15-minute versions exist
but were not loaded (about 20,000 more requests at the one-a-second the server accepts).

### Historical lines

ESPN keeps, per game, each book's last line, which is the closing line. For some seasons it
also keeps an opening line. Loaded per season (games with a final score):

| Season | Games | Opening line | Closing spread | Closing moneyline | Main closing books |
|---|---|---|---|---|---|
| 2015-16 | 1,312 | 1,312 ("Opening" pseudo-book) | 1,312 | 1,312 | Bovada, BetOnline |
| 2016-17 | 1,304 | 1,270 | 1,270 | 1,263 | Bovada, BetOnline |
| 2017-18 | 1,308 | **0** | 1,308 | 1,308 | Westgate |
| 2018-19 | 1,310 | **0** | 1,310 | 1,310 | Caesars |
| 2019-20 | 1,143 | **0** | 1,143 | 1,143 | DraftKings, Caesars |
| 2020-21 | 1,147 | **0** | 1,147 | 1,147 | DraftKings, BetMGM |
| 2021-22 | 1,319 | **0** | 1,319 | 1,319 | DraftKings, BetMGM |
| 2022-23 | 1,313 | **0** | 1,313 | 1,313 | DraftKings |
| 2023-24 | 1,314 | 1,313 (each book's open) | 1,314 | 1,314 | DraftKings, ESPN BET |
| 2024-25 | 1,310 | 1,310 | 1,309 | 1,310 | ESPN BET |
| 2025-26 | 1,313 | 1,313 | 1,313 | 1,313 | DraftKings, ESPN BET |

**The gap.** 2017-18 to 2022-23, which include both validation seasons, have no line known
before tip, only the close. Consequences:
- **Market group:** it could not be judged on validation, so it is not in the model (below).
- **Thresholds:** chosen on validation bets graded at the closing line and price (the M12/M13
  convention).
- **Final test:** graded both at the opening line and at the close.
- **Live:** lines collected from now on (ESPN's DraftKings line hourly, plus keyed sources)
  close the gap (BACKLOG.md).

## Prediction points (`src/betdata/nba/points.py`)

| Point | When (US Eastern) | What is new |
|---|---|---|
| `morning` | 14:00 on game day: the first hourly run after the first official report of game day (1:30 PM in the three-a-day era; teams submit game-day statuses between 11 a.m. and 1 p.m. local). For a tip before 15:00, three hours before tip (not before 07:00). | Game-day injury statuses |
| `early` (live only) | any hourly run before `morning` | Shown on the page; predicted with the morning fit; not graded separately in backtests |
| `pretip` | the last hourly run (on the hour, 07:00-22:00) strictly before tip | The latest injury report, the latest line |

How often a player's status changes between the two points is measured and recorded.

## As-of rules

| Data | `known_at` | Basis |
|---|---|---|
| Schedule: teams, tip time, site | always known | Published before the season |
| Final score, player box score | tip + 3 h (history); the fetch time (live) | Rule: a game is over about 2.5 h after tip |
| Availability inferred from a box score (a rotation player with zero minutes) | the same game's box score `known_at` | Known only after the game; never a pre-game input for that game |
| Injury report row | the report's stated time + 15 minutes (history); the fetch time (live) | Real publish time plus a posting margin |
| Opening line (history) | 07:00 ET on game day | Rule: NBA openers are posted the evening before |
| Market line (live) | `fetched_at` of the poll snapshot | Closing lines are never inputs |

The as-of harness (`src/betdata/nba/asof.py`) is M12's: every feature group is rebuilt at a
point from all data, from only the rows known then, and from all data with every later row
scrambled, and must give identical values.

## Seasons and acceptance rule

* **Train** 2015-16 to 2020-21 (seasons 2015-2020). **Validate** 2021-22 and 2022-23. **Test**
  2023-24 to 2025-26, scored once at the end after refitting on 2015-2022.
* **Base model**: home court plus an Elo rating difference for the margin; the league's
  rolling scoring level for the total.
* Groups are added one at a time in the order below. A group is **kept** if, added to the kept
  set, it lowers validation margin MAE (total MAE for the pace group), averaged over both
  points. One build plus at most 3 targeted adjustments per group; every result is logged.
* Ridge regression on standardised features, as NFL. The simpler model wins unless another is
  clearly better on validation.
* Win probability Phi(margin / sigma). Calibration is checked on validation; one Platt fit is
  applied only if the model is overconfident.
* Final test: margin and total MAE against an Elo baseline and against the closing line where
  lines exist; spread and moneyline bet records with 95% ranges and ROI at thresholds chosen on
  validation; Brier score against the no-vig market.

## Feature groups

1. **Player availability**: player value from box scores (minutes-weighted plus/minus and
   scoring, shrunk to a replacement level); play probability per report status learned on
   training seasons; stars separately; several rotation players out together; players
   returning from long absences.
2. **Team strength**: opponent-adjusted offensive and defensive ratings with recency weights,
   and ratings rebuilt from the players expected to play.
3. **Rest and schedule**: back-to-backs (which leg), games in the last N days, rest
   difference, 3-in-4 and 4-in-6, travel miles, time zones crossed, altitude (DEN, UTA), long
   road trips, first home game after a trip.
4. **Pace** (totals; kept in the CLI).
5. **Home court** per season, including the 2019-20 bubble and 2020-21 limited crowds.
6. **Context**: early-season priors decaying with games played; late-season tanking and resting
   (clinched or eliminated teams); In-Season Tournament games.
7. **Market**: the line at the prediction point and its movement since the open. Model-only and
   model + market blend are both reported. The closing line is never an input.

## M14 results log

Everything below is reproducible from the data (`betdata nba load-history <nba-history files>`,
then `betdata nba select | tune | final-test | freeze`). The reports are committed under
`reports/nba/`.

### How the work was judged

- **Development runs:** while the history backfill was still running, the pipeline was debugged
  on incomplete injury and line data (2022-23 and 2024-26 partly loaded). Those numbers were not
  used for any decision.
- **The official runs below:** all on the complete data.
- **Adjustments:** one targeted adjustment was made, to group 2 (below). No other group was
  adjusted.

### Play probabilities by status (learned on training seasons 2015-16 to 2020-21)

A rotation player's chance of playing, as learned from what happened at both prediction points
(`play_prob` in the frozen fit):

| Official status at the point | P(plays) | Player-points seen |
|---|---|---|
| Out | 0.5% | 20,109 |
| Doubtful | 5.2% | 1,019 |
| Questionable | 50.3% | 5,123 |
| Probable | 88.8% | 3,008 |
| Available | 86.6% | 1,449 |
| Not on a report that lists his team; played last game | 92.5% | 111,403 |
| ... missed his team's last 1 / 2-4 / 5+ games | 52.7% / 37.2% / 14.9% | 8,420 / 7,910 / 7,326 |
| No report for the game (2015-18); played last game | 90.6% | 186,257 |
| ... missed last 1 / 2-4 / 5+ | 47.2% / 28.7% / 10.1% | 16,767 / 18,037 / 20,452 |
| "Removed" (dropped from a later report) | 5.4% | 35 (too few to trust) |

Box-score absences (a rotation player with no minutes) only count once the game is over, and
only for later games. "Available" (86.6%) sits below the unlisted rate (92.5%). The likely
reason, not tested, is that it mostly marks players coming back from an injury.

### Group by group (validation 2021-22 and 2022-23, MAE averaged over both points)

| Step | Margin MAE | Total MAE | Morning / pre-tip margin | Decision |
|---|---|---|---|---|
| Base (home court + Elo) | 10.722 | 15.391 | | |
| + 1 availability | **10.588** | 15.408 | 10.611 / 10.565 | kept (−0.134) |
| + 2 team strength | **10.553** | 15.186 | 10.570 / 10.536 | kept (−0.035) |
| + 3 rest and schedule | **10.519** | 15.186 | 10.533 / 10.505 | kept (−0.034) |
| + 4 pace (totals) | 10.519 | **14.680** | | kept on total (−0.506) |
| + 5 home court by season | 10.534 | 14.680 | | dropped (+0.015) |
| + 6 context | **10.516** | 14.680 | 10.531 / 10.502 | kept (−0.003) |
| + 7 market at the point | 10.530 | 14.665 | | dropped: no line before tip in validation (above) |

For comparison on validation:
- **Elo baseline:** 10.773.
- **Closing line:** 10.280 (pre-tip, 2,632 games).
- **Closing total:** 14.109.

Ablation from the final model (validation):

| Without | Margin MAE | Total MAE |
|---|---|---|
| (final model) | 10.516 | 14.680 |
| availability | 10.579 (+0.063) | 14.679 |
| team strength | 10.544 (+0.028) | 14.964 (+0.284) |
| rest and schedule | 10.549 (+0.033) | 14.679 |
| pace | 10.516 | 15.186 (+0.506) |
| context | 10.519 (+0.003) | 14.680 |

**Adjustment 1 of 3, team strength.** The rating step was raised from 0.04 to 0.06 (faster ratings). Margin went from 10.553 to 10.566 (worse), so it was reverted; no further adjustments were made.

**Home court (dropped).**
- **What it tried:** a slow-moving estimate of home edge, plus bubble and limited-crowd flags.
- **Why it adds nothing:** the base model's home term already carries the average.
- **The decline the spec mentions is real:** the estimate averaged 2.3-3.4 points a season in 2015-16 to 2019-20, then 1.3 in the limited-crowd 2020-21 season, 1.7-2.7 in 2021-23 and 1.8-2.3 in 2023-26. It moves too slowly to beat a constant on two validation seasons.

**Context.** Its 0.003 gain is the smallest kept effect and sits within noise. It is kept under the acceptance rule. The early-season prior, roster continuity and tanking terms carry it.
- **Coaching changes:** no free source has them, so they are not modelled.
- **In-Season Tournament games:** not flagged; the schedule rows don't mark them (BACKLOG.md).

**Model.** Ridge regression with a margin penalty of 3 and a total penalty of 1,000; sigma is 13.28 on the final fit. No gradient-boosting challenger was built for NBA in M14 (BACKLOG.md), so the simple model is the only one.

### Calibration (validation)

- **Platt fit:** slope 0.975 with intercept −0.021, on both points pooled.
- **Applied:** yes. The rule applies one fit only when the model is overconfident, and slope < 1 means it was, slightly.
- **Brier score on validation:** 0.2196.

| Predicted | Games | Predicted (mean) | Home won |
|---|---|---|---|
| 0.1-0.2 | 79 | 0.165 | 0.203 |
| 0.2-0.3 | 243 | 0.261 | 0.230 |
| 0.3-0.4 | 530 | 0.355 | 0.355 |
| 0.4-0.5 | 935 | 0.453 | 0.428 |
| 0.5-0.6 | 1,253 | 0.551 | 0.586 |
| 0.6-0.7 | 1,078 | 0.650 | 0.657 |
| 0.7-0.8 | 764 | 0.743 | 0.726 |
| 0.8-0.9 | 336 | 0.836 | 0.818 |
| 0.9-1.0 | 42 | 0.919 | 0.857 |

### Thresholds and blend (validation; frozen in `config/nba_model.json`)

Spread bets on validation, at the closing line:

| Edge ≥ | Bets | W-L-P | ROI |
|---|---|---|---|
| 1 | 3,669 | 1,805-1,808-56 | −4.6% |
| 2 | 2,440 | 1,219-1,185-36 | −3.2% |
| 3 | 1,483 | 726-731-26 | −4.8% |
| 4 | 823 | 412-388-23 | −1.6% |
| 5 | 444 | 229-206-9 | +0.5% |
| **6** | **216** | **120-94-2** | **+7.0%** |

- **Spread threshold:** chosen as **6 points**, the best ROI with at least 100 bets.
- **Moneyline threshold:** chosen as **5 % pts**, at the closing price: +2.0% on 2,607 bets (2 pts +0.5%, 3 +1.8%, 4 +1.9%, 6 +1.3%, 8 −0.1%).
- **Blend weight:** **0.2** model / 0.8 line.
  - It needs a line known before tip, which only 2015-16 and 2016-17 have, so it was chosen there.
  - The model for that choice was fitted on 2017-18 to 2020-21 only.
  - MAE was 9.62 for the line alone, 9.61 for the 0.2 blend, and 9.86 for the model alone.

### Final test: 2023-24 to 2025-26, scored once (`reports/nba/final_test.json`)

The frozen design was refitted on 2015-16 to 2022-23. There were 3,937 games, and every one had a closing line.

| Margin MAE | Morning | Pre-tip | Both |
|---|---|---|---|
| **Model** | 10.987 | 10.943 | **10.965** |
| Elo baseline | 11.349 | 11.349 | 11.349 |
| Opening line (the market at the morning point) | 10.983 | 10.983 | 10.983 |
| **Closing line** | 10.761 | 10.761 | **10.761** |
| Model + market blend (0.2 × model + 0.8 × opening line) | 10.941 | 10.927 | 10.934 |
| Total MAE: model / closing total | 14.839 / 14.296 | 14.843 / 14.296 | 14.841 / 14.296 |

Bets at the validation thresholds (spread ≥ 6 pts at −110; moneyline ≥ 5 % pts at the price):

| | Bets | W-L-P | Win rate (95% range) | ROI | Avg CLV |
|---|---|---|---|---|---|
| **Spread at the closing line** | 293 | 151-141-1 | 51.7% (46.0-57.4%) | **−1.3%** | 0 by construction |
| Spread at the opening line | 482 | 302-180-0 | 62.7% (58.3-66.9%) | +19.6% | +4.1 pts |
| **Moneyline at the closing price** | 3,763 | 1,429-2,334 | 38.0% (36.4-39.5%) | **−7.0%** | — |
| Moneyline at the opening price | 3,900 | 1,758-2,142 | 45.1% (43.5-46.6%) | +4.6% | — |

Brier score, both points: model **0.2043**, no-vig closing market **0.1990**.

**Reading the bet rows.**
- **The opening-line rows are not achievable results.** The opener is a stale line. By the morning or pre-tip point, the market has already moved on the same injury news the model reads. That is the +4.1 points of "closing-line value".
- **The realistic grade lies between the two rows and is closer to the close.** Measured at the close, spread bets are break-even minus vig, and moneyline bets lose 7%.
- **Part A:** the moneyline final-test ROI used for flagging is the closing-price figure, **−7.0%**. So NBA moneyline edges are shown as information only and are never flagged, as for NFL.

**Re-scoring after a bug fix (REVIEW.md M14-4).**
- **The bug:** inspecting the first live cards showed that last season's end-of-year absences carried into each opening game.
- **The fix:** applied identically to training, validation and test, with the frozen design unchanged.
- **Re-scored with `--again`:**
  - Margin MAE: 10.966 → 10.965.
  - Spread at the close: −0.9% → −1.3%.
  - Moneyline at the close: −7.3% → −7.0%.
  - Both runs are committed (`final_test_first_scoring.json`).
- **What was not done:** no threshold, group or penalty was re-chosen after the test was seen.

**Two more live findings (REVIEW.md M14-5, M14-6).**
- **The name-map fix (M14-6):** changed neither validation nor test to 4 decimals. The fit was refrozen.
- **The rejected roster carry-over (M14-5):** carrying last season's rosters into a new season until each team plays was also tried, and rejected because validation got worse (10.516 → 10.520). It was scored on the test seasons in the same run (11.017, spread −6.7% and moneyline −9.3% at the close), so it is reported here. That test score played no part in the decision.
- **Reported results:** the numbers above are the current design's.

### How often statuses change between morning and pre-tip

For players listed on a report by the pre-tip point, the share whose status at pre-tip differs
from the morning (including players first listed after the morning):

| Season | Games | Players listed | Changed | Uncertain → Out | Uncertain → Available |
|---|---|---|---|---|---|
| 2018-19 (from Dec) | 860 | 6,632 | 13.1% | 134 | 117 |
| 2019-20 | 1,125 | 10,172 | 13.3% | 194 | 199 |
| 2020-21 | 1,135 | 8,671 | 15.7% | 170 | 195 |
| 2021-22 | 1,315 | 12,920 | 19.3% | 459 | 537 |
| 2022-23 | 1,295 | 11,748 | 21.1% | 502 | 672 |
| 2023-24 | 1,295 | 13,216 | 23.7% | 647 | 827 |
| 2024-25 | 1,295 | 14,269 | 24.3% | 615 | 846 |
| 2025-26 | 1,292 | 14,183 | 24.7% | 921 | 995 |

Statuses settle later every season: a quarter of listed players now change between 2 PM and the last run before tip. The model is correspondingly better at pre-tip:
- Validation: 10.502 vs 10.531.
- Test: 10.943 vs 10.987.

### Frozen live fit

- **Fit:** `config/fits/nba_fit_2015_2025.json`, on every completed season 2015-16 to 2025-26 with the frozen design, including the training-season play probabilities.
- **Version:** shown on the page as `nba:<hash>`.
- **No hourly refit:** the hourly runner only reads it.

### 2026-27 replay (`betdata nba replay`, run on Actions by `nba-report.yml`)

The frozen model replays the new season's finished games. Each game gets a prediction at its morning and pre-tip points, using only what was stored by then, and that prediction is compared with the market line known then and with the result. Output goes to the `nba-report` branch (`replay_2026.json`). Run 2026-10-21, after opening night:

| Game | Point | Model (home margin) | Market | Result | Error |
|---|---|---|---|---|---|
| BOS @ DET | morning | DET −0.8 | – | DET −8 | 8.8 |
| BOS @ DET | pretip | DET −0.4 | – | DET −8 | 8.4 |
| PHI @ NYK | morning | NYK +7.8 | – | NYK −6 | 13.8 |
| PHI @ NYK | pretip | NYK +8.2 | – | NYK −6 | 14.2 |

- **Too few games to judge:** two games, MAE 11.3 at both points against 10.9-11.0 on the test seasons.
- **No market comparison yet:** both games tipped before the runner first stored an NBA line (NBA went live in the evening of Oct 20).
- **From here on:** every game has the DraftKings line from ESPN at each point, and the replay grows as the season does. Re-run the workflow to update it.
- **Opening-night limitation (BACKLOG):** the rotations are last season's until a team's first game is over.

# M15: new side variables and player props

## M15 evaluation rules (Part A)

- **Side models are decided on validation only.** NFL uses 2020-2022 and NBA uses 2021-22 and 2022-23, the M12 and M14 splits. The test seasons have been seen before (NFL 2023-2025 twice, NBA 2023-24 to 2025-26 once). Each new side version is scored on them once, labelled **repeat look** (third for NFL, second for NBA), and nothing is adjusted afterwards.
- **Page switch rule.** The page moves to a new side version only if it improved validation AND did not worsen the test result (margin MAE at every prediction point). Otherwise the current version stays on the page, and the new one is predicted, stored and graded in the background.
- **Live grading.** Every new version, sides and props, is stored and graded live from now on. 2026-27 onward is the only unseen data left, and live CLV is the judge.
- **Budget per variable group and per prop market:** one full build plus at most 3 targeted adjustments on validation. A group is kept only if it lowers validation error. Every result is recorded below, kept or dropped.
- **Frozen versions stay frozen.** m12, m13w and the M14 NBA version (`nba:8d4d776447`) are not changed: not their code paths, fits or configs. New variables ship as new versions (`m15` for NFL, `nba15` for NBA) beside them.

## M15 data sources

All were checked from a GitHub Actions runner on 2026-10-21 (`probe/m15-sources`).

| Dataset | Where | Reachable | Coverage | Licence / attribution |
|---|---|---|---|---|
| NFL participation (players on field, pressure, time to throw, coverage man/zone) | nflverse-data `pbp_participation` | yes | 2016-2025; **not yet published for 2026** | CC-BY 4.0 (nflverse), data from NFL NGS |
| FTN charting (blitzers, pass rushers, play action, screen, QB fault sack) | nflverse-data `ftn_charting` | yes | 2022-2026 (2026 updated weekly) | CC-BY-SA 4.0, attribute FTN Data |
| Next Gen Stats (separation, cushion, time to throw) | nflverse-data `nextgen_stats` | yes | 2016-2026 (combined files, updated daily) | CC-BY 4.0 (nflverse); NFL NGS |
| PFR advanced stats (pressures, hurries, blitzes; def coverage) | nflverse-data `pfr_advstats` | yes | 2018-2026 weekly | CC-BY 4.0 (nflverse); Pro Football Reference |
| Depth charts | nflverse-data `depth_charts` | yes | 2001-2024 weekly (one format); 2025-2026 daily snapshots (new ESPN-based format: `dt, team, pos_abb, pos_rank`) | CC-BY 4.0 (nflverse) |
| Player weekly stats (incl. kicking/punting, penalties) | nflverse-data `stats_player` | yes | 1999-2026 | CC-BY 4.0 (nflverse) |
| Play-by-play (EPA, special teams, penalties, third down, red zone) | nflverse-data `pbp` (already used) | yes | 1999-2026 | CC-BY 4.0 (nflverse) |
| NBA play-by-play with substitutions (lineups rebuilt from them) | sportsdataverse-data `espn_nba_pbp` | yes | 2002-2026 seasons; current season from ESPN game summaries (`plays`) | MIT (sportsdataverse); data from ESPN |
| NBA player box scores | sportsdataverse + ESPN summaries (M14) | yes | 2015-16 on | as M14 |
| ESPN player prop lines | `sports.core.api.espn.com/.../competitions/{id}/odds/{provider}/propBets` | yes (keyless, unofficial) | see below | ESPN; unofficial API, used at ≤1 request/s; no redistribution of raw data |
| NBA referee crews | none stored (M14.1 was not run) | – | – | skipped; BACKLOG |

**ESPN prop line history.** The coverage scan sampled two games a month, every provider ESPN lists:

- **NBA:**
  - none in 2021-22 or earlier;
  - from 2022-23 (DraftKings, Caesars, MGM, SugarHouse; ESPN BET from Dec 2023), points, rebounds, assists and threes with over and under labelled and priced, opening and final price;
  - points + rebounds + assists from 2024-25;
  - 2025-26: ESPN BET until about Nov 2025, then DraftKings (provider 100) with gaps: Dec, Feb, Mar and Apr samples were empty;
  - 2026-27: DraftKings, live.
- **NFL:**
  - 2022-2023: touchdown-scorer specials only, no yardage;
  - 2024-2025: ESPN BET passing, rushing and receiving yards, receptions and anytime TD. The main line has over and under prices; alternate milestone lines carry no prices;
  - 2026 (DraftKings): lines with **no prices**.
- **Item format.** Every item is one side of one line. Labelled items (`current.over` / `current.under`) are the main line. DraftKings 2026 items come in unlabelled pairs per line, the first being the over: 144 of 148 pairs agree with DraftKings' own milestone prices. Alternate ladders (one-sided, mostly unpriced) are ignored.

**What that allows:**
- **NBA points, rebounds, assists and threes:** lines in validation (2022-23) and test seasons, so a real backtest with thresholds chosen on 2022-23.
- **NBA PRA:** lines from 2024-25 only (test), so accuracy-only.
- **NFL (all markets):** no lines in validation, so no betting backtest is possible. The test seasons (2024-2025) are compared on error vs the line only.
- **Live:** prop lines are stored every hourly run from now on, for live grading.

**As-of rules for the NFL files** (`nfl/nflverse_files.py`; a row whose time can't be placed gets no time and is never used):

| Data | `known_at` / `as_of` | Basis |
|---|---|---|
| Participation, FTN charting, PFR advanced, player weekly stats (per game) | kickoff + 12 h | M12 rule for a game's stats and play-by-play |
| NGS weekly rows | the team's game that week, kickoff + 12 h | same rule |
| NGS season totals (week 0), and weeks matching no game (NGS numbers the Super Bowl week 23) | the team's last game of the season, kickoff + 12 h | conservative: a running total is never used before the season ends |
| Depth chart, weekly format (2001-2024), week N | the team's week-N `prekick`; a team without a game that week (bye): that week's last kickoff | as weekly rosters: the weekly file may hold game-week changes, so before `prekick` the week N-1 chart is used |
| Depth chart, daily format (2025+) | the snapshot's own `dt` | real capture time |

Depth-chart positions are normalized to QB RB FB WR TE LT LG C RG RT (OL when the side isn't given) DE DT LB CB S K P LS H KR PR, with groups QB RB WR TE OL DL LB DB ST; rank 1 is the starter.

## M15 NFL side variables (`nfl/features/m15_*.py`, version `m15`)

All use only plays and games before the prediction point, which the M12 harness checks. They are exponentially weighted with the M12 strength half-life and regressed to the league mean by sample size.

1. **Line matchups.**
   - *Pass:* offense pressure rate allowed, sack rate and time to throw vs defense pressure rate and sack rate. Pressure and time to throw come from participation (2016-2025); 2026 has none, so pressure falls back to PFR weekly pressures (2018+), and sack rate comes from pbp.
   - *Run:* offense rushing EPA before contact is not free, so rush EPA/success vs defense rush EPA/success from pbp.
   - *OL continuity:* starts by the same five linemen (snap counts, OL position), plus OL starters out (injury report, as M12 availability).
2. **Depth-chart backups.** Every position group (QB, RB, WR, TE, OL, DL, LB, DB): when a starter is out, the value of the actual next player on the most recent depth chart before the point, by his own snap-weighted EPA share. M12 only did this for QB. Depth charts before the point: weekly for 2016-2024 and the latest daily snapshot for 2025-2026.
3. **Special teams.** Kicker FG accuracy vs expected by distance (pbp `field_goal_result`, `kick_distance`), punting net EPA, return and coverage EPA per play (pbp `special_teams_play`).
4. **Situational luck.**
   - Third-down conversion rate vs expected (a logistic on distance) and red-zone TD rate vs expected (by yard line), offense and defense.
   - The difference is regressed toward 0 like M12's turnover luck: the feature is the part expected to revert.
5. **Penalty discipline.** Accepted penalties and yards per play, offense and defense (pbp `penalty_team`).

### M15 NFL build notes (`nfl/m15data.py`, `nfl/features/m15_*.py`)

- **Inputs.** Three frames are added beside M12's, all stamped as M12 stamps a game's stats (kickoff + 12 h): `m15_tg` (per team-game counts from pbp 2011-2026 and the nflverse files), `m15_kick` (field goals per kicker per game) and `m15_depth` (depth charts; known at the chart's `as_of`). M12's frames and groups are unchanged.
- **Expected rates**, logistic fits on training seasons 2013-2019 only: FG make = 1/(1+exp(-(6.236 - 0.1095 x distance))); third-down conversion = 1/(1+exp(-(0.918 - 0.823 x ln(yards to go)))); red-zone trip TD = 1/(1+exp(-(1.375 - 0.0775 x yard line of the first snap inside the 20))).
- **Pressure fallback.** Participation `was_pressure` where the game has it (2016-2025); else PFR `times_pressured` x 1.147 (2018+, used for 2026); else (sacks + QB hits) x 1.218 (2011-2015). Both factors make the fallback match participation on training games that have both. Time to throw: participation, else NGS weekly passing (2026); 2011-2015 have none and regress to the league mean.
- **Regression and weighting.** Every rate uses M12 strength's half-life (120 days), look-back (600 days) and between-season keep (0.7), and is shrunk toward the league with a prior: 150 dropbacks/carries (lines), 30 punts / 30 kickoffs / 40 returns / 20 FG attempts (st), 100 third downs / 20 red-zone trips (sitluck), 300 snaps (pen).
- **Depth value** = a player's recency-weighted share of his unit's snaps over his games, shrunk toward 0 with 2 games. A starter counts as missing only when the injury data flags him (report, prekick inactive, reserve), weighted by 1 - M12's play probability.
- **Data gaps.** No 2010 play-by-play file, so `m15_tg` starts in 2011 (well before the first training game's 600-day window matters). Depth-chart starters match a snap-count player 99%+ of the time from 2013 (60-89% in 2010-2012, before snap history). Participation is not published for 2026.

### M15 NFL selection (validation 2020-2022, `betdata nfl select15`; `reports/nfl/m15_selection.json`)

Start: the page version m13w (M12 + weather in the margin model), fitted on 2013-2019, 838 validation games, MAE averaged over both points, ridge penalty chosen on validation from M12's grid. M12's rule: a group's margin half is kept if it lowers margin MAE, its total half if it lowers total MAE. A dropped full build gets up to 3 targeted adjustments; a kept build ends the group.

| Step | Margin MAE | Total MAE | Penalty m/t | Decision (margin / total) |
|---|---|---|---|---|
| start (m13w design) | 9.819 | 10.734 | 1000/300 | start |
| + lines, full build | 9.821 | 10.783 | 1000/1000 | drop / drop |
| + lines, adj. 1: pass protection + OL only (rush terms overlap M12 strength) | 9.820 | 10.782 | 1000/300 | drop / drop |
| + lines, adj. 2: prior x3 (450) | 9.823 | 10.791 | 1000/1000 | drop / drop |
| + lines, adj. 3: pressure matchup + OL starters out only | 9.822 | 10.757 | 1000/300 | drop / drop: **lines dropped** |
| + depth, full build (7 position groups) | 9.821 | 10.763 | 1000/300 | drop / drop |
| + depth, adj. 1: one feature summed over groups | **9.818** | 10.745 | 1000/300 | KEEP / drop (-0.001, noise) |
| + st, full build | **9.782** | 10.768 | 300/300 | KEEP / drop (-0.036) |
| + sitluck, full build | 9.817 | **10.733** | 1000/300 | drop / KEEP (-0.001, noise) |
| + pen, full build | **9.778** | 10.752 | 300/300 | KEEP / drop (-0.004) |

Final draft (`config/nfl_model_m15.json`): margin = m13w margin groups + depth (summed, version 101) + st + pen, penalty 300; total = M12's total groups + sitluck, penalty 300. Validation MAE 9.778 / 10.733 (final report 9.807 / 10.743, prekick 9.749 / 10.723), against 9.819 / 10.734 for m13w. Not tested on 2023-2025 and not used live.

Ablation (each kept M15 group removed, penalties fixed at 300/300):

| | Margin | Total |
|---|---|---|
| all kept | 9.778 | 10.733 |
| without depth | 9.779 (+0.001) | 10.733 |
| without st | 9.823 (+0.045) | 10.733 |
| without sitluck | (not in margin) | 10.734 (+0.001) |
| without pen | 9.782 (+0.004) | 10.733 |

Special teams carries nearly all of the gain. Depth and situational luck are within noise and kept only by the rule. The line-matchup group never helped: pressure and rushing efficiency are mostly already in M12's opponent-adjusted EPA ratings.

### M15 NFL final test: third look at 2023-2025 (`betdata nfl final-test15`; `reports/nfl/m15_final_test.json`)

m15 and m13w were refitted on 2013-2022 with their frozen designs and scored once on the 855 test games. M12 and M13 scored these seasons before, so this is the third look. Nothing was changed after the numbers were seen.

| MAE (855 games) | m15 | m13w (rerun) | m13w (recorded M13 test) | closing line |
|---|---|---|---|---|
| Margin, final report | 10.082 | 10.059 | 10.057 | 9.786 |
| Margin, prekick | 10.005 | 9.995 | 9.992 | 9.786 |
| Total, final report | 10.374 | 10.379 | 10.377 | 10.120 |
| Total, prekick | 10.366 | 10.375 | 10.374 | 10.120 |

The rerun of m13w differs from its recorded test by 0.002-0.003. Its features were rebuilt from this machine's database and caches, not the runner's.

Bets at the close:

| | Spread (≥ 3 pts), final report | Spread, prekick | Moneyline (≥ 3 % pts, M13 config), final report | Moneyline, prekick |
|---|---|---|---|---|
| m15 | 137-118-5, +2.5% ROI | 138-113-4, +4.9% | 267-321-1, -7.1% | 269-313-1, -5.6% |
| m13w | 110-99-4, +0.5% | 105-93-3, +1.2% | 234-336-0, -8.1% | 225-337-1, -9.6% |

The 95% ranges of every spread record include 50%. Brier on decided games, prekick: m15 0.2167, m13w 0.2166, no-vig market 0.2102.

**Page switch: no.** Validation improved (9.778 < 9.819), but the test margin MAE is worse than m13w's at both points (+0.025 at the final report, +0.013 at prekick). The page stays on m13w. m15 is written to `config/nfl_model_m15.json` with `page: false`. Its live fit, on 2013-2025 and frozen by `betdata nfl freeze15`, is `config/fits/nfl_m15_fit_57fcaa5663_2013_2025.json`. m15 is predicted, stored (model `m15`) and graded hourly in the background.

**Live inputs.** `nfl/live15.py` reads m15's inputs for the last three seasons from compact per-season tables under `data/derived`, which the runner's state keeps (`nfl/m15cache.py`):
- per team-game counts and kicker field goals, computed with the frozen expected-rate fits;
- this season's depth charts, keeping a daily snapshot only when a team's chart changed.

A season's table is rebuilt when its play-by-play or nflverse file is on disk and newer than the table. On the runner that happens in the 7am run (`nfl-inputs15`); the one-off `nfl-m15-bootstrap` workflow builds the earlier seasons. Tables built this way give exactly the same features as reading the files: checked on the 14 games of 2026-10-21.

The run is skipped (logged, nothing stored) when the inputs are incomplete:
- last season is missing;
- fewer than 90% of this season's games that finished more than 3 days ago are present (the play-by-play of the latest games arrives with the next 7am refresh);
- no depth chart is known from the last 10 days.

m15 reuses the M12 store and the computed states that m12 and m13w have just used, so it adds about 6 s to an hourly run.

## M15 NBA side variables (`nba/features15.py`, version `nba15`)

1. **Lineup RAPM.**
   - *Stints:* rebuilt from ESPN play-by-play substitutions. Starters come from the box score; later periods' starters are inferred from who acts before being subbed in.
   - *Fit:* a ridge regression of points per 100 possessions on the players on court, fitted with `scipy.sparse` (already installed as a dependency of scikit-learn; no new package) on games before each Monday (weekly refits; live: the latest Monday).
   - *Prior:* a box-score per-minute prior for players under 250 minutes.
   - *Team strength:* the minute-weighted sum of expected available players' RAPM (play probability × projected minutes).
2. **Three-point shooting luck.** Team 3P% and opponent 3P% vs the league, over the last 15 games, regressed toward the mean with a 750-attempt prior. Defensive 3P% allowed is mostly luck.
3. **Load management.**
   - Each star's chance of sitting is learned on training seasons by situation: second night of a back-to-back, national TV (ESPN broadcast field), opponent below .400, and after Mar 15 with seeding settled (the team's playoff position locked by standings).
   - "Star" means top 2 on his team by minutes × usage.
   - It is used **only while the player is not on the injury report** for that game. Once the report lists him, availability takes over.
4. **New-player integration.** Games since each rotation player joined the team. Rotation minutes added and lost in the last 30 days (trades and signings) as a share of team minutes.
5. **Style matchups.** All from box scores, last 15 games, regressed:
   - offensive rebound rate vs opponent defensive rebound rate;
   - 3PA rate vs opponent 3PA allowed;
   - turnover forcing vs ball security;
   - FT rate vs opponent foul rate.

## M15 NBA lineup stints (`nba/stints.py`, table `nba_stints`)

Stints are stretches with the same ten players on the floor, rebuilt from ESPN play-by-play: sportsdataverse season files for history (`betdata backfill --source sportsdataverse --sport nba --dataset pbp --seasons 2015-2025`, box scores first), and each newly finished game's ESPN summary live (the `espn_nba boxscores` job). `betdata nba bootstrap` loads the last three seasons where they have none.

- **Period 1** starts with the box score's five starters per team.
- **Later periods:** a player who records any event in the period (technicals and ejections excepted) or is subbed out before being subbed in started it. Fewer than five: the rest come from the previous period's closing lineup, leaving out anyone subbed in during the period, only if exactly the missing number is left.
- **Unresolved:** more than five found, an ambiguous fill, or a substitution that doesn't fit the lineup. That period's stints are skipped, counted and logged.
- **Dead balls:** all substitutions at one clock instant are applied together, after the other plays at that instant, so free throws belong to the lineup of the foul.
- **Points:** changes in the running score. **Possessions:** FGA − player offensive rebounds + turnovers (travels included) + 0.44 × FTA (technical free throws excluded), per team.
- **Time:** seconds elapsed in the game (720 s periods, 300 s overtimes). `known_at`: tip + 3 h for history, the fetch time live.
- **Skipped games:** two ESPN games that the schedule matcher joined into one game (same teams within 36 hours, mostly 2020-21's two-game series) get no stints.

Sanity checks (2026-10-21, local database, stints vs `results`):

| Season | Games with stints | Stints per game (median, min-max) | Fully resolved | Stint points = final score | Skipped: shared game / no game |
|---|---|---|---|---|---|
| 2015-16 | 1,305 | 30 (13-51) | 99.1% | 98.8% | 8 / 1 |
| 2020-21 | 1,123 | 29 (19-44) | 99.6% | 99.6% | 48 / 1 |
| 2024-25 | 1,299 | 30 (17-50) | 99.5% | 99.5% | 22 / 3 |
| 2025-26 | 1,303 | 32 (15-50) | 99.2% | 99.2% | 18 / 4 |

- **Points:** in every fully resolved game, stint points add up to the play-by-play's own final score. The few fully resolved games that differ from `results` (4 in 2015-16, 1 in 2025-26) are games where the file's running score itself ends elsewhere.
- **Possessions:** summed per team-game, they run 0.6 ± 1.0 above the box-score formula in 2024-25 (team turnovers count here; box scores leave them out). In 2015-16 they run 1.8 ± 2.8 below, because some plays are missing from the file.

## M15 NBA player props (`nba/props/`, version `nbap15`)

Markets: points, rebounds, assists, made threes, and points + rebounds + assists. The model is built per player-game, as of each prediction point (morning and pre-tip, as M14).

1. **Projected minutes.** A ridge regression, trained on 2015-16 to 2020-21, on:
   - recent minutes (last 3, 10, season), starter role and rest or back-to-back;
   - games since returning from an absence (restriction);
   - fouls per minute;
   - blowout risk: the M14 frozen model's projected margin;
   - teammates' expected missing minutes at his position.
2. **Per-minute rates with redistribution.**
   - The player's rates (last 10, season) and usage share.
   - When teammates are out or questionable (weighted by 1 − play probability), their share of the team's stat is reassigned in proportion to the player's share.
   - A per-player "with/without" boost is learned from this team's earlier games without that teammate, shrunk heavily to the league response.
3. **Game environment.** The M14 frozen model's predicted total and pace relative to the league.
4. **Opponent.** Stat allowed per minute to the player's position (G/F/C) relative to the league, last 20 games, shrunk strongly (half weight at 40 games).
5. **Referee crews:** not stored (M14.1 not run). Skipped and logged in BACKLOG.

- **Output:** mean μ and a negative binomial distribution per stat, with dispersion fitted per market on training seasons. The output includes P(over line).
- **Calibration:** over probabilities are checked on validation and one Platt fit is applied only if they are miscalibrated.
- **Baselines:** the last-10-game average (naive) and, where lines exist, the line itself.
- **Backtest:** markets with lines in validation (points, rebounds, assists, threes):
  - edge thresholds (probability points over the no-vig line) are chosen on 2022-23;
  - the test is 2023-24 to 2025-26, graded at the final ESPN line and price;
  - each market reports its record, 95% range, ROI and Brier vs no-vig.
- **PRA:** lines start in 2024-25, so it is accuracy-only and marked "research only, not backtested".

## M15 NFL player props (`nfl/props/`, version `nflp15`)

Markets: passing yards, rushing yards, receiving yards, receptions, and anytime touchdown (a probability of at least one rushing or receiving TD). The model is built per player-game at the M12 points (`final_report`, `prekick`; live also `early`, read as the final report like M12).

**No betting backtest was possible.** No NFL prop lines exist for the validation seasons (2020-2022), so no edge threshold can be chosen. Every market is judged on prediction error against a naive baseline and on the calibration of P(over), and every market is "research only, not backtested" (`config/nfl_props.json`: `research_only` = all five, no thresholds, no test ROI; never flagged).

**Inputs** (`nfl/props/data.py`, `panel.py`; each row known at kickoff + 12 h, read only if known by the point):

1. **Role:** snap share (M12's snap counts), route participation (on the field for a dropback, from participation `offense_players`; nflverse's `route` column is only the targeted receiver's route, so it can't give routes run; 2016-2025, snap share where missing, e.g. 2026), target, air-yards, carry and pass-attempt shares, red-zone targets and carries (play-by-play, `yardline_100 <= 20`). Last 6 games, season so far, and exponentially weighted (half-life 3 games). Rates (yards per target, catch rate, yards per carry and per attempt) over the last 16 games, shrunk to the position's league rate (40 targets/carries, 100 attempts).
2. **Game script:** team pass attempts, targets and carries from a ridge on the team's last-6 and season averages, the opponent's pace, the side model's margin and total, and wind. The side input is the frozen m13w design (its total model is M12's) refitted so no season's input comes from a fit that saw it: each training season from a fit on the other training seasons, validation from 2013-2019, test from 2013-2022 (`nfl/props/side.py`). Live: the latest stored m13w prediction (m12 if none).
3. **Teammate injuries:** M12's candidates and play probabilities by status (`features/availability.py`, learned on 2013-2019 and stored in the fit). A teammate's chance of missing is `q = 1 - p_play / p_healthy`, where `p_healthy` is M12's probability for a player with no status at that point (0.84 at the final report, 0.89 at prekick). Using `1 - p_play` raw would count every healthy teammate as 16% missing. His last-6 share of targets, carries or attempts times `q` is the missing share `m`, and the player's expected share if he plays is `share / (1 - m)`: with one teammate surely out the others' shares add back up to the team total (tested).
4. **Opponent:** receiving yards and receptions allowed per target to WRs, TEs and RBs, rushing yards per carry and passing yards per attempt, over the defence's last 10 games, relative to the league's last ~320 team-games, shrunk strongly (120 targets / 150 carries / 200 attempts of league average). Man/zone: the defence's man rate (participation `defense_man_zone_type`, which starts in 2018; FTN charting has no coverage column) vs the league, times the player's own man-minus-zone yards per route, each shrunk to his overall rate with 150 routes. 2026 has no participation file, so man/zone is 0 live.
5. **Weather:** the M13 `wx` group's wind above 10 km/h for passing and receiving yards and team volume. **Deviation:** this machine has only nflverse's observed wind (1999-2021). The Open-Meteo forecasts (2022+) live in the runner's state, and Open-Meteo is unreachable from here. So 2022-2025 are scored with no wind. There is no precipitation history at all (nflverse has none), so the rain term was dropped.

**Model** (`nfl/props/model.py`): volume x share x rate, as the NBA prop model's minutes x per-minute rate. Each market's ridge uses these terms:
- team volume x the last-6, season and EW shares x the rate;
- for receivers, route participation x targets per route, and air-yards share;
- teammates' missing share (`share x m / (1 - m)`; passing yards: receivers out);
- opponent factor;
- wind;
- man/zone;
- own Questionable/Doubtful status;
- the last-10 average.

Anytime TD uses its own terms: red-zone touches (last 6, season, x the team's implied points), TDs per game (last 6, season, last 16) and redistribution.

**Distributions:**
- **Yards:** negative binomial or a continuity-corrected normal, with variance a + b·μ + c·μ² fitted on training residuals. The family is picked on validation by Brier at the synthetic line, and negative binomial won for all three markets. It handles the right skew of yards; the normal over-predicts overs for receivers.
- **Receptions:** negative binomial.
- **Anytime TD:** Poisson on the expected count, so P(anytime) = 1 − e^−λ.

**Populations** (who each market is scored on, as known before the game):
- passing yards: QBs with at least 15 attempts a game over the last 6;
- rushing yards: at least 3 carries a game;
- receiving yards and receptions: WR/TE/RB with at least 2 targets a game;
- anytime TD: non-QBs with at least 3 carries + targets a game.

Live predicts every player with a line.

### How it was judged (`betdata nfl props-select`, `reports/nfl/props_selection.json`)

**Setup:**
- Fit on 2013-2019, scored on 2020-2022.
- **Baseline:** the last-10-game average. For anytime TD it is the player's season TD rate, giving P = 1 − e^−rate.
- **Calibration:** P(over) at a synthetic line, the last-10 average rounded down + 0.5.
- **Per market:** one full build plus three adjustments, the same three for every market. Each market keeps the variant with the lowest validation MAE (anytime TD: the lowest Brier, since the MAE of a mostly-zero count rewards predicting 0).

**Platt rule** (decided before looking): one Platt fit per market on the validation predictions, applied only if a calibration bin with at least 300 predictions is off by more than 0.03, or the n-weighted mean gap is above 0.015. All five markets met it: passing and rushing yards under-predicted overs by 3-5 points in the middle bins.

| Validation | n | MAE model | MAE baseline | RMSE model | RMSE baseline | Brier (P(over)) | Brier constant | Mean calibration gap → after Platt |
|---|---|---|---|---|---|---|---|---|
| passing yards | 3,412 | **65.80** | 69.02 | **83.17** | 87.52 | 0.2363 | 0.2487 | 0.032 → 0.017 |
| rushing yards | 9,004 | **21.98** | 23.03 | **29.33** | 30.56 | 0.2330 | 0.2414 | 0.041 → 0.009 |
| receiving yards | 20,280 | **21.75** | 22.27 | **28.56** | 29.30 | 0.2317 | 0.2374 | 0.017 → 0.016 |
| receptions | 20,280 | **1.638** | 1.677 | **2.086** | 2.133 | 0.2361 | 0.2425 | 0.016 → 0.013 |
| anytime TD (count; P(≥1)) | 20,822 | 0.437 | **0.428** | **0.565** | 0.613 | **0.1812** (baseline 0.2013) | 0.1916 | 0.013 → 0.007 |

Normal vs negative binomial (validation Brier):

| Market | Normal | Negative binomial |
|---|---|---|
| passing yards | 0.2365 | 0.2363 |
| rushing yards | 0.2385 | 0.2330 |
| receiving yards | 0.2448 | 0.2317 |

| Adjustment (validation MAE; TD: Brier) | pass yds | rush yds | rec yds | receptions | anytime TD | Kept for |
|---|---|---|---|---|---|---|
| Full build (ridge penalty 10) | **65.80** | **21.98** | **21.75** | 1.6384 | **0.1812** | pass, rush, rec yds, TD |
| 1. Ridge penalty 100 | 65.88 | 22.00 | 21.75 | **1.6382** | 0.1812 | receptions |
| 2. Ridge penalty 1000 | 66.24 | 22.09 | 21.78 | 1.639 | 0.1812 | – |
| 3. Fewer share terms (EW and route shares only) | 66.85 | 22.10 | 21.86 | 1.646 | 0.1813 | – |

### Final test: 2023-2025, scored once (`betdata nfl props-final-test`, `reports/nfl/props_final_test.json`)

The validated design was refitted on 2013-2022 with the validation Platt fits and scored once on 2023-2025.

| Test | n | MAE model | MAE baseline | RMSE model | RMSE baseline | Brier at synthetic line | Brier constant | Mean calibration gap |
|---|---|---|---|---|---|---|---|---|
| passing yards | 3,602 | **69.70** | 70.71 | **88.33** | 89.04 | 0.2480 | 0.2499 | 0.038 |
| rushing yards | 9,202 | **21.14** | 22.03 | **28.25** | 29.45 | 0.2317 | 0.2407 | 0.019 |
| receiving yards | 19,562 | **21.56** | 22.01 | **28.53** | 29.14 | 0.2343 | 0.2391 | 0.015 |
| receptions | 19,562 | **1.590** | 1.637 | **2.038** | 2.090 | 0.2323 | 0.2420 | 0.007 |
| anytime TD | 20,118 | 0.429 | **0.420** | **0.556** | 0.611 | **0.1811** (baseline 0.2068) | 0.1931 | 0.009 |

**Reading the test:**
- The model beats the last-10 average in every season for every yards and receptions market.
- Passing yards gains least (1.0 yard of MAE). Its calibration got worse on test (mean gap 0.038), so its P(over) is the least trustworthy.
- Anytime TD loses on MAE, as on validation (a count that is mostly 0), but wins clearly on RMSE and Brier.
- **Lines:** no prop lines were loaded when the test was scored ("no lines loaded"; the ESPN BET 2024-2025 history is a separate backfill into `prop_lines`). Once loaded, `line_comparison` in `nfl/props/final.py` compares the model and the line on the same player-games: MAE, side hit rate, and Brier vs the no-vig line.

**Frozen fit:** `betdata nfl props-freeze` fits on 2013-2025 and writes:
- `config/fits/nfl_props_2013_2025.json`, the model with M12's play probabilities;
- `config/nfl_props.json`.

**Live** (`nfl/props/live.py`):
- **Who and when:** every NFL game in the next 8 days, for players with a stored prop line, at the game's current point; stored change-only in `prop_predictions` as `nflp15`.
- **Factors:** the top two, in the stat's units, e.g. `["wind 25 km/h", -4.2, "yds"]` or `["Kelce out", 3.0, "yds"]`.
- **Plugin:** the NFL plugin runs props after the m12/m13w predictions; a failure is logged and never breaks the run.
- **Runner state:** the hourly runner keeps `data/derived` but not the nflverse or play-by-play files, which its 7am jobs fetch. So the prop panel caches each season's player-game rows (`derived/nfl_prop_box_<season>.parquet`), rebuilt whenever that season's stats and play-by-play files are newer, and the ESPN→gsis map (`derived/nfl_espn_gsis.json`). Gap: last season's files are never fetched on the runner. Until a full season has been cached there, a rebuilt state has only this season's games as history (rates over fewer games, shrunk harder to the league).
- **ESPN → gsis ids** (`nfl/props/ids.py`): nflverse's daily ESPN-based depth charts (2025+) carry both ids. Players missing there are matched on a unique normalised name among players with nflverse stats, where the line has a name (ESPN NFL prop items carry only the athlete id, so in practice the depth charts do the mapping). Coverage on 2026-10-20: the latest snapshot gives a gsis id for 2,022 of its 2,028 ESPN ids (587 of 588 QBs, RBs, WRs and TEs), and 511 of the 512 QBs, RBs, WRs and TEs with a 2026 stat line have an ESPN id this way.

**Grading** (`nfl/props/grade.py`):
- **Actuals:** nflverse weekly player stats. A new daily `nfl-player-stats` job refreshes this season's file; nflverse publishes overnight, so a game is graded the morning after.
- **No stat line:** a player with no stat line but offensive snaps is graded 0; with the game's snaps in and none for him, `void`; otherwise pending.
- **Depth charts:** the `nfl-depth-charts` job keeps the ID map current.

## M15 grading and page

- **Prop grading:** `betdata grade nba --props` and `betdata grade nfl --props` grade every stored prop prediction against the box score into `prop_grades`:
  - over, under or push;
  - void if the player did not play;
  - CLV vs the closing prop line, or none if there is no closing line.
- **Props section:** each sport tab gets one, listing players with a stored line. Each row shows:
  - projection, line, P(over) and edge;
  - top 2 factors;
  - source and fetch time.
- **Prop flags:** only for markets whose test-season backtest ROI was positive. Markets without a backtest are labelled "research only, not backtested" and never flagged.
- **Side cards** name the version used and any version running in the background.

## M15 NBA props results (`betdata nba props-select | props-tune | props-final-test`)

**Error vs the last-10 average, validation 2021-22 and 2022-23** (172k player-game-points, both prediction points):

| Market | MAE model | MAE last 10 | RMSE model | RMSE last 10 |
|---|---|---|---|---|
| minutes | 5.060 | 5.362 | | |
| points | 4.615 | 4.733 | 5.985 | 6.202 |
| rebounds | 1.917 | 1.968 | 2.513 | 2.600 |
| assists | 1.293 | 1.321 | 1.756 | 1.815 |
| threes | 0.873 | 0.878 | 1.216 | 1.254 |
| PRA | 6.040 | 6.240 | 7.732 | 8.057 |

At synthetic half-point lines (no market), P(over) is well calibrated in every bin with volume.

**ESPN prop line history.** Loaded by the `props-history` workflow: 2022-23 to 2025-26, two books a game, about 280k rows a season for 2022-23 and 2023-24, fewer after.
- **The "close" in ESPN's history is in-game** (REVIEW M15-3), so it is unusable.
- **The opening line is pre-game:** its over rate is 44-49% in every market and all four books agree. The backtest therefore uses the opening line and price; few closes quoted before tip exist (reported separately).

**Validation 2022-23, at the opening line** (`reports/nba/props_tune.json`):
- **Model vs the market:** the over probabilities carry no information against the points line; model-minus-line has a correlation of −0.002 with actual-minus-line. Against rebounds, assists and threes the correlation is weak but real (0.11 for rebounds).
- **Calibration:** each market was miscalibrated (Platt slopes −0.003 points, 0.23 rebounds, 0.38 assists, 0.49 threes), so one Platt fit was applied to each, per the rule.
- **Thresholds chosen:** points 6, rebounds 10, assists 10, threes 8 % pts.

**Final test 2023-24 to 2025-26, scored once** (`reports/nba/props_final_test.json`, pre-tip predictions at the opening line):

| Market | Rows | MAE model | MAE line | Brier model | Brier no-vig | Bets (W-L) | Win rate (95%) | ROI |
|---|---|---|---|---|---|---|---|---|
| points | 39,309 | 5.053 | 5.020 | 0.2499 | 0.2441 | 1,228-2,876 | 29.9% (28.5-31.3) | −18.2% |
| rebounds | 38,895 | 2.023 | 2.130 | 0.2425 | 0.2378 | 1,722-3,912 | 30.6% (29.4-31.8) | −14.2% |
| assists | 37,204 | 1.519 | 1.600 | 0.2394 | 0.2353 | 2,304-4,586 | 33.4% (32.3-34.6) | −7.9% |
| threes | 34,717 | 1.091 | 1.176 | 0.2357 | 0.2314 | 4,228-6,581 | 39.1% (38.2-40.0) | −3.2% |
| PRA | 18,399 | 6.517 | 6.746 | 0.2424 | 0.2333 | research only (lines from 2024-25) | – | – |

- **Mean error:** the model's mean projection is closer to the result than the opening line for rebounds, assists, threes and PRA, but not points.
- **Probabilities and bets:** its over probabilities are worse than the market's (Brier), and every backtested market lost money. The bets' low win rates show the calibrated edges mostly pick long-priced sides.
- **Flags:** none for any prop market (positive test ROI required). Every prop is shown as information only, and PRA is labelled research only.

### M16 audit of this backtest (REVIEW M16-1, `reports/nba/props_final_test_m16_audit.json`)

**Question:** win rates of 30-39% sat beside ROIs of only −3% to −18%. Was the arithmetic wrong?

**Answer: the arithmetic was right, but the lines were wrong.**
- There were no pushes or voids, and each ROI matches its win rate at the prices bet.
- The bets were long prices: median +175 to +200, and 10% at +500 or longer.
- On the side it bet, the model's probability averaged about 0.50 against a no-vig price of about 0.32.

**Cause:** the ESPN parser (`sources/espn_props.main_line`) took the *first* labelled over/under pair as the main line. On many players, ESPN also labels alternate ladder lines. So 4-31% of stored history rows (by season and book; ESPN BET 2024 opening lines: 31%) were an alternate line priced like +300/−450 and stored as "main". Against such a line, a 10 % pt "edge" is simply the distance to the alternate.

**Fixes:**
- The parser now takes the most balanced labelled pair.
- The backtest keeps only lines whose no-vig over is between 0.35 and 0.65, before choosing the book.

**Re-score:** the frozen `nbap15` was scored again on the cleaned lines, using its own frozen calibration and thresholds. This is a re-score after a data-bug fix, not a refit or a re-tune.

| Market | Rows | MAE model | MAE line | Brier model | Brier no-vig | Bets (W-L) | Win rate (95%) | ROI |
|---|---|---|---|---|---|---|---|---|
| points | 37,064 | 5.062 | 4.913 | 0.2501 | 0.2493 | 744-1,092 | 40.5% (38.3-42.8) | −10.7% |
| rebounds | 34,997 | 2.025 | 2.049 | 0.2472 | 0.2472 | 1,011-980 | 50.8% (48.6-53.0) | +10.6% |
| assists | 32,042 | 1.542 | 1.556 | 0.2466 | 0.2456 | 1,443-1,593 | 47.5% (45.8-49.3) | +5.0% |
| threes | 28,485 | 1.115 | 1.146 | 0.2454 | 0.2428 | 3,348-3,921 | 46.1% (44.9-47.2) | +0.9% |
| PRA | 15,062 | 6.546 | 6.333 | 0.2583 | 0.2487 | research only | – | – |

**Checks:**
- On the cleaned lines, always betting the over loses 8-10% in every market, and always betting the under loses 2-5%, as the vig predicts.
- Win rates now sit where the prices say they should.

**Reading:**
- The model's probabilities are still no better than the market's (Brier equal or worse).
- The positive ROIs are at *opening* lines. Rebounds' comes almost entirely from unders (93% of its bets), while assists win on both sides. They are one test, not proof of an edge.
- Re-tuning on the cleaned validation lines would move the assists and threes calibration (threes Platt slope 0.49 → 0.33).

**Page unchanged:** M16 forbids changing frozen fits and the hourly page's behaviour. So `config/nba_props.json` (fit, calibration, thresholds and the M15 `test_roi` that gates flags) is **not** changed. NBA props stay information only, with no flags. Whether rebounds or assists should be flagged on the corrected numbers is left to a later phase, which would need a fresh, untouched test.


### Prop flags after the M16 audit (decision 2026-10-27)

The M15 rule is applied unchanged to the corrected final-test numbers: a market is flagged only if its final-test ROI is positive.

- **Source:** the frozen `nbap15`, scored pre-tip at the opening line on the cleaned 2023-24 to 2025-26 lines (`reports/nba/props_flags_m16.json`).
- **Range:** the 95% range is mean ± 1.96·sd/√n of the 1-unit bet profits.
- **Unchanged:** the thresholds (validation 2022-23), the fit and the calibration. Only `test_roi` in `config/nba_props.json` changes.

| Market | Threshold | Bets (W-L) | ROI | 95% range | Flag |
|---|---|---|---|---|---|
| points | 6 pts | 744-1,092 | −10.7% | −15.6% to −5.7% | no |
| rebounds | 10 pts | 1,011-980 | +10.6% | +5.7% to +15.4% | **yes** |
| assists | 10 pts | 1,443-1,593 | +5.0% | +1.1% to +9.0% | **yes** |
| threes | 8 pts | 3,348-3,921 | +0.9% | −1.7% to +3.6% | no: **info only** (see below) |
| PRA | - | research only | - | - | no |

- **Info only (owner's decision, 2026-10-27):** a market is flagged only when its whole 95% range is above zero. A positive ROI whose range includes zero is shown on the page as "info only (backtest ROI range includes 0)" and never flagged. Threes (+0.9%, −1.7% to +3.6%, 7,269 bets) is info only; rebounds (1,991 bets) and assists (3,036 bets) stay flagged. `config/nba_props.json` carries `test_roi_95`; `PropDesign.flag_allowed` checks it.
- **Caveat:** these are opening lines, and the rebounds edge is almost all unders. The flags are graded live like every other pick.
- **Model version:** `nbap15` hashes only the frozen fit, so it does not change.

## M15 summary: what changed on the page

| Sport | Page side version | Background (graded live) | Why |
|---|---|---|---|
| NBA | `nba15` (M14 + RAPM + 3-point luck + load) | M14 `nba` | Validation 10.516 → 10.468; test (second look) 10.888 vs 10.965, better at both points |
| NFL | `m13w` (unchanged) | `m15` (m13w + depth + special teams + penalties; situational luck in totals) | Validation 9.819 → 9.778, but the test (third look) was worse at both points (+0.025 final report, +0.013 prekick) |

**Props** (both tabs) are shown for players with a stored ESPN line, as information only:
- No NBA market had a positive test ROI.
- NBA PRA and every NFL market have no backtest ("research only, not backtested").

# M16: alerts and closing-line value (written 2026-10-22, before any live M16 data)

M16 changes no model. It runs the frozen versions (nba15 and M14, m13w and m15, nbap15, nflp15) faster than hourly when injury news breaks, compares soft books with a sharp reference, alerts the owner, and grades every alert against the pre-game close. Everything below is **fixed now**. Nothing is tuned during the study.

## Status changes that qualify (the "value threshold")

**NBA.** A change in a player's official-report status for a game (Out, Doubtful, Questionable, Probable, Available, or newly listed or removed) qualifies when the player is in his team's current rotation with an expected **≥ 20 minutes per game played** (`min_play`, M14 League state) **and** the change moves his M14 play probability by **≥ 0.25** (e.g. Questionable 0.50 → Out 0.01).

**NFL.** An injury or inactive status change qualifies for:
- a QB with ≥ 50% of offensive snaps over his last 4 games;
- any other offensive skill player (RB, WR, TE) with ≥ 60%;
- any offensive lineman or defensive starter with ≥ 80%;

where the status change moves his M12 play probability by **≥ 0.25**.

## Signal 1: news (model moved by injury news vs the current DraftKings/FanDuel line)

When a qualifying change is detected, only that game is re-predicted, with the frozen page version (NBA nba15, NFL m13w) and the frozen prop model. The game alerts when all of these hold:
- **Side (spread):** the model's home margin moved by **≥ 1.0 point** because of the news (re-prediction vs the last stored prediction), AND |model margin + DK/FD home spread| ≥ **3.0 points** (NBA) / **2.5 points** (NFL) in the direction of the move, at the current DK or FD line.
- **Moneyline:** no Signal 1 alert. Spreads and props cover the news.
- **Props (NBA points, rebounds, assists, threes; NFL receiving and rushing yards, receptions)** for the affected player's teammates (and the player himself when he moves to Questionable or Available):
  - the projection moved by **≥ 10%**;
  - P(over | no push) minus the no-vig over at the current DK line is **≥ 8 percentage points**, in the direction of the move.

  If the DK prop line has no prices (NFL DK in 2026), no prop alert is sent.

## Signal 2: soft vs sharp (DraftKings/FanDuel vs Pinnacle)

**Sharp reference:** the Pinnacle no-vig price, from The Odds API (region `eu`) or SportsGameOdds, whichever delivers Pinnacle on its free plan (PROJECT.md, M16 sources). Markets are full-game moneyline, spread and total for NBA and NFL games starting within the polling window.

A soft price alerts when, on the same market and the same line:
- the soft book's no-vig probability for a side is **≥ 3.0 percentage points below** Pinnacle's no-vig probability for that side; AND
- the expected value at the soft book's actual price, using Pinnacle's no-vig probability, is **> 0** (p_pin × decimal_soft − 1 > 0).

Spreads and totals compare only identical lines; different lines are skipped. Pinnacle quotes older than 10 minutes, or soft quotes older than 5 minutes, are not compared.

## Alerts

- **Content:** game, market, book, price and line, the reason (e.g. "Embiid → Out at 1:30 PM; FD line unchanged since 1:12 PM"), and a time-stamped link to the page.
- **Delivery:** POST to ntfy.sh/<NTFY_TOPIC>, a secret that is never logged or committed.
- **De-duplication:** at most one alert per (game, market, side, signal, triggering change). Signal 1's triggering change is the status change. Signal 2's is the (soft price, Pinnacle price) pair, re-alerted only after the gap closed below threshold and reopened.
- **Quiet hours:** no pushes from 23:00 to 07:00 ET. Signals found then are stored as paper bets (`sent=false`) but not pushed.
- **Paper bets:** every alert, pushed or quiet, is a paper bet of 1 unit at the alerted price and line.

## Lag study (Part E, the go/no-go test for Signal 1)

**Events:** every qualifying status change detected by the poller over two weeks of live games.

**Logged per event:**
- the report time (stated time and when we read it);
- every poll's DK, FD and Pinnacle prices for that game's spread, moneyline and total, and DK prop lines for affected players, for the next 60 minutes.

**A book's lag:** minutes from the report's publication (the report's stated time, else our first read) to the first poll where that book's line or no-vig price on the affected market has moved by **≥ 0.5 points** (spread or total) or **≥ 1.5 percentage points** (moneyline or prop no-vig). If nothing moved within 60 minutes, the event is censored at 60.

**Rule, fixed now:** if the **median soft-book lag (DK and FD pooled) is under 5 minutes**, Signal 1 is turned off and logged as **not viable on free data**. The poller cannot beat a book that moves within one polling cycle plus SharpAPI's 60-second delay. Signal 2 continues either way.

The decision is taken once, at the end of the two weeks. Before then Signal 1 runs, with its paper bets labelled as part of the study.

## Grading and success rule (Part F)

**`betdata grade alerts`** grades each alert against the **pre-game close**: the last line stored before tip from the same book; else, for Signal 2, Pinnacle's last pre-game line.

**Recorded per alert:**
- CLV in points (spreads, totals, props);
- CLV in no-vig probability (all markets);
- the result (win, loss, push or void) and profit at the alerted price.

**Reports** are split by signal, sport and market: average CLV with a 95% range (normal approximation, mean ± 1.96·sd/√n), and ROI.

**Success rule, fixed now:** after **200 graded alerts per signal**, a signal is "working" only if its average no-vig CLV is **positive and its 95% range excludes zero**. Before 200, no verdict is given.

**Implementation note (2026-10-22, fixed before any graded alert; `src/betdata/alerts/grade.py`).** How the rules above are computed:
- **Close:** the last line stored strictly before tip (the main database's current start time, else the alert's) from the alert's book and market (and player, matched by normalized name): the poller's quotes (SharpAPI, The Odds API) and the main database's polls (`odds_snapshots`, full game, active prices; `prop_lines` kind `poll`), whichever is later. Each source's last row of the bet side is paired with that source's last row of the other side at the matching line for the no-vig; without a pair, no no-vig CLV. Signal 2 falls back to Pinnacle when the soft book has no pre-game line; Signal 1 has no fallback.
- **CLV points:** spreads, bet line − close line (each the side's own line); overs, close − bet; unders, bet − close. None for the moneyline.
- **CLV no-vig** (stored as a probability, reported in % points): the close's no-vig probability of the bet side minus the book's no-vig probability for that side at alert time (`alerts.book_prob`; if missing, the no-vig of the alert's price and `details.other_price`, else none). When the close line differs from the bet line, the close probability is carried to the bet line with a normal approximation: the side's stat ~ N(mu, sigma), mu set so the close line gives the close no-vig, i.e. p_bet = Φ(Φ⁻¹(p_close) + CLV points / sigma) (push mass ignored). Fixed sigmas: NBA spread 12.0, NBA total 18.0, NFL spread 13.5, NFL total 13.0; props max(1.0, 0.5·√line + 0.15·line) at the close line.
- **Result and profit:** 1 unit at the alerted price and line. Game markets from the final score (`results`, final); props from the box score (NBA `nba_player_games`: points, oreb + dreb, ast, fg3m; NFL nflverse weekly player stats, matched by name, with snap counts telling 0 from did-not-play). Void (profit 0) for a postponed or cancelled game or a player who did not play. ROI = profit / settled bets (wins, losses, pushes; voids excluded).
- **Counting toward the success rule:** an alert counts as graded for the verdict when it has a no-vig CLV; the 95% range uses the sample sd (n − 1).
- A grade is rewritten only when a later run has more (a result after a close); a grade with a result and a close is final.

# M17: an NFL engine built bottom-up (written 2026-10-27, before any M17 result)

M17 builds a second NFL model that never reads a betting line. It predicts each game from the plays up: how many plays each team runs, how many are passes, where the targets and carries go, how efficient each is, and how drives end. It then simulates the game 10,000 times. It ships as two **new versions** beside the current ones:

- **`nfle17`** for sides;
- **`nflpe17`** for props.

m12, m13w, m15 and nflp15 are not changed: not their code, fits, configs or stored predictions. The alerts layer, the append-only odds history and existing migrations are not changed either.

**The market is never an input.** No spread, total, moneyline, prop line or price enters any engine input, fit, calibration or hyper-parameter choice. Lines are read only after the engine has predicted, to compare (Part D) and to grade. A test checks that the engine package imports nothing that reads lines (`odds_snapshots`, `prop_lines`, `quotes`, `nfl/features/market.py`).

## Seasons and rules (fixed now)

- **Sides:**
  - **Train** 2016-2019 (participation data starts in 2016): every rate, padding and coefficient is fitted here.
  - **Validate** 2020-2022: every choice of window, padding multiplier, drive-model term or threshold is made here.
  - **Test** 2023-2025: scored once, after refitting on 2016-2022 with the frozen design. It is labelled **repeat look (fourth for NFL sides)**: M12, M13 and M15 each scored these seasons once. Nothing is adjusted afterwards.
- **Props:**
  - The player model is fitted on 2016-2019 and its windows are chosen on 2020-2022 by MAE, as nflp15 was. No lines exist for those seasons.
  - Betting thresholds need lines, and ESPN's NFL prop lines exist only for 2024 and 2025 (M15). So **2024 is the prop validation season** (thresholds chosen there) and **2025 the prop test season**, scored once.
  - nflpe17's player model is refitted on 2016-2023 before 2025 is scored. This is declared now: no prop threshold can be chosen on 2020-2022.
- **Stop rule:** per engine step, one full build plus at most 3 adjustments, each judged on validation. Every result is logged below, kept or dropped. Never adjust after seeing a test season.
- **As-of:** every input row carries `known_at` (a game's plays: kickoff + 12 h, as M12). A prediction at instant `t` reads only rows known by `t`. The engine predicts at the M12 points (`final_report`, `prekick`; live also `early`, read as `final_report`).
- **Free data only, no new keys.** Network access stays in `fetch`, through `core/http.py`. UTC is used in storage and Eastern for display.

## Part A: measure first

**A1. Stability table** (`reports/nfl/stability.csv`, `betdata nfl engine stability`). For every rate the engine uses (team pace, neutral pass rate over expected, sack and scramble rates, pressure rate, target share, targets per route, catch rate, yards per target, aDOT, YAC per reception, carry share, yards per carry, red-zone shares, drive-outcome rates, FG make rate, punt net, and the defence's allowed versions):

- **Split-half reliability:** odd vs even games within a season, for units with at least 4 games in each half. Spearman-Brown corrects it to a full season.
- **Season-to-season reliability:** the correlation of a unit's season rate with its next-season rate (units with enough volume in both seasons).
- **Empirical padding:** the number of opportunities `k` at which the reliability is 0.5, from the split-half reliability `r` at the median sample size `n`: `k = n·(1 − r)/r`. An estimate after `m` opportunities is then shrunk as `(m·x + k·prior)/(m + k)`, with the prior the position (or league) mean. Rookies use a prior by draft-round bucket (step 10).
- **Which seasons feed the engine:** columns are computed on 2016-2019 (these feed the engine) and on 2016-2025 (reported only).

**A2. Data timing.** For each nflverse file the engine could use (play-by-play, participation, FTN charting, PFR advanced, NGS, player stats, depth charts, rosters), this records:
- whether the current season's file exists in season;
- when it is updated, from the release's asset timestamps, checked on GitHub Actions.

**Routes:** participation (players on the field for each dropback) gives real routes run, but has no 2026 file in season (M15). Two target models are compared on 2020-2022 (validation) for predicting next-game targets:
- (a) targets per route run, with real routes;
- (b) targets per *proxy route*, where proxy routes = snap share × team dropbacks.

The engine uses (b) live whenever participation is missing. The comparison measures what the proxy costs.

**A3. Pressure flag check.** Pressure per dropback from participation (`was_pressure`, 2016-2025) is compared with PFR's `times_pressured` (2018-2026), per team-game and per QB-season: correlation, mean level and the split-half reliability of each. The engine's sack step uses PFR (it is published in season) unless the check shows it disagrees with participation (team-game correlation below 0.7). In that case pressure is left out and sacks are modelled from sack rates alone.

**A4. Coordinators** (`seeds/coordinators_nfl.csv`, 2016-2026, 32 teams × 11 seasons):
- **Rows:** head coach, offensive coordinator, defensive coordinator, and a mid-season change with the week it took effect, where the page states one.
- **Source:** English Wikipedia's team-season articles ("2023 Buffalo Bills season", infobox fields `coach`, `off_coach`, `def_coach`), fetched via the MediaWiki API from GitHub Actions at one request a second.
- **Licence:** CC BY-SA 4.0, so every row carries its source URL and revision id, and the file's header gives the attribution.
- **Not used:** Pro-Football-Reference is not scraped.
- **Checks:**
  - **40 random spot-checks:** each row is compared with the coordinator's own Wikipedia article (career entries naming the team and the years). Mismatches are listed and resolved by hand from the cited revision.
  - **Persistence analysis:** the season-to-season correlation of team pace, neutral pass rate over expected and the defence's pressure and blitz rates, split by "same offensive (defensive) coordinator and head coach" vs "changed". If persistence is clearly lower after a change (difference in r > 0.1 with ≥ 60 team-seasons in each group), the engine's prior for a new season regresses those rates harder for teams with a new coordinator. Otherwise coordinators are not used.

## Part B: the engine, step by step (`src/betdata/nfl/engine/`)

Each step estimates rates from play-by-play known at the prediction point. Estimates are exponentially weighted over the current and previous season, shrunk with the A1 padding, and carried across seasons with the A1 season-to-season reliability. Each is opponent-adjusted where the step names a defence: the defence's allowed rate relative to the league, shrunk with its own padding.

1. **Team plays.** Seconds per play in neutral situations (win probability 0.2-0.8, first and second down, outside the last two minutes of a half), for offence and for the opponent's defence. The drive model (step 7) turns pace into plays and drives; no play total is predicted directly.
2. **Pass vs run.** Neutral pass rate over expected (nflverse `xpass`, `pass_oe`) for the offence, plus the defence's allowed version. In the simulation the pass probability of each play is the league curve `xpass(down, distance, field position, score, time)` shifted by the team's over-expected rate. Game script therefore moves it.
3. **Dropbacks.** Each dropback ends in a sack (offence and QB sack rate, the defence's pressure or sack rate), a QB scramble (QB scramble rate) or a pass attempt.
4. **Targets.** Each receiver gets a share of attempts: his target share, built from route share × targets per route (real or proxy routes, A2) and shrunk to a position prior. Players who are out are removed and the shares renormalised. Air yards (aDOT) is the player's, shrunk.
5. **Receiving efficiency.** Catch probability from completion over expected (the player's, shrunk, applied to the league's catch curve by aDOT) and the QB's completion over expected. Yards after catch per reception: the player's, shrunk. Both are adjusted for the opponent's yards per target allowed.
6. **Rushing.** Designed runs are split by carry share among RBs, QB (designed) and others. Yards per carry come from the rusher's rate (shrunk hard: it is unstable, A1), the team's line (team yards before contact, where available, else team success rate) and the opponent's run defence.
7. **Touchdowns and points: a drive model.** A drive ends in a touchdown, field goal attempt, punt, turnover, turnover on downs or end of half. A multinomial logit is fitted on 2016-2019 drives. Its inputs are:
   - the start field position;
   - the offence's and defence's expected points per play (as-of, shrunk), split into pass and run by the drive's pass share;
   - the half's time left and the score;
   - the offence's QB.

   Plays and seconds per drive come from the result (negative binomial plays, pace-scaled seconds). A touchdown is a passing or rushing TD by the red-zone pass rate. It goes to a player by red-zone target or carry share (QB rushes included). Field goals are made with the league's make curve by distance (kicker-adjusted, shrunk). Extra points are made at the league rate.
8. **Defence and special teams.** Defence enters through the allowed rates in steps 1-7. Special teams enter through the start field position after kickoffs (touchback and return distribution) and punts (team net punt, shrunk), and the field goal curve. Kicking props are out of scope.
9. **Availability.** Every player has M12's play probability at the point (`nfl/features/availability.py`; live, the as-of store with the latest statuses). Each simulation draws who plays. A missing player's targets and carries go to the teammates who play, by their shares. A missing starting QB is replaced by the backup's own shrunk rates, or by a replacement-level prior from backup QBs 2016-2019.
10. **Rookies and new players.** A player with no NFL history starts from a prior by position × draft-round bucket (1, 2, 3, 4-7, undrafted) from rookie seasons 2016-2019, using nflverse `draft_picks` and `players`. It is then updated with his own games and the A1 padding.

## Part C: simulation (`nfl/engine/sim.py`)

- **Volume:** at least 10,000 simulations per game. They are vectorised (numpy), drive by drive, with a running score and clock.
- **Correlated script:** pass probability, pace and the drive model all read the simulated score and time. A team that leads runs more, slows down and gives the trailing team more passing plays. Correlations between teams and players follow from this, not from an imposed matrix.
- **Overdispersed counts and skewed yards:**
  - Targets per player are Dirichlet-multinomial: shares vary game to game, with the A1 between-game variance.
  - Receptions are binomial on the targets.
  - Receiving yards are a sum of skewed per-catch gains (gamma, mean and shape fitted per position on 2016-2019).
  - Rushing yards per carry come from a fitted two-part distribution (a loss or short gain, plus a long-tail gamma).
- **Outputs per game:**
  - the home margin and the total (mean, median, 10th and 90th percentiles);
  - P(home wins), P(home covers any line);
  - each team's points, plays, pass attempts, sacks and drives;
  - per player: median and mean passing yards, rushing yards, receiving yards, receptions, P(anytime TD), and P(over) at any line.
- **Consistency checks** (tests, on every live run, plus a history report): per team and simulation,
  - targets add up to pass attempts;
  - completions = receptions;
  - receiving yards = passing yards;
  - rushing TDs + passing TDs = offensive TDs;
  - points = 6·TD + XP + 3·FG (+ safeties, 2-point tries);
  - no player above his team's total;
  - medians within the 10th-90th range.

  On history, the means of plays, pass rate, sacks, points and TDs per team-game are compared with the actuals of the same seasons (bias within 3%).
- **Runtime:** the hourly NFL engine step (every game in the 8-day window, 10,000 simulations each, props included) must take **under 3 minutes** on the GitHub runner. This is proven from the run logs of live hourly runs.

## Part D: engine vs market (`nfl/engine/value.py`)

- **Gap:** engine minus market. Spread: the engine's home margin + the home spread. Total: engine total − line. Props: engine median − line, and P(over) − the no-vig over.
- **Decomposition by chain step:** the game is re-simulated with each step's inputs set to the league average, one step at a time, with the same random numbers. A step's contribution is the full prediction minus that run. The page shows the top three steps per game, e.g. "pass rate +1.4, receiving efficiency −0.8".
- **Thresholds:** chosen on validation only.
  - **Sides:** spread edge in {1, 1.5, 2, 2.5, 3, 4} points against the nflverse closing line on 2020-2022, with at least 100 picks. The threshold with the highest validation ROI at −110 is chosen, if positive. Otherwise the engine flags no sides.
  - **Props:** P(over) − no-vig edge in {4, 6, 8, 10, 12} pp on 2024 lines, at least 60 bets per market, the same rule.
- **The three tests** (validation and test separately; 95% intervals):
  1. **Do gaps predict results?** Cover rate and ROI by gap size, and a logistic regression of the cover on the gap. Props: over rate vs P(over) − no-vig.
  2. **Do gaps predict line movement toward the engine (CLV)?** Regress (closing − opening line) on the gap at the opening line; also the share of games where the close moved toward the engine. Opening lines: ESPN's per-book opening and closing lines (the core odds API used for NBA in M14), fetched by a new `nfl-lines-history` workflow on Actions for 2016-2025 as far back as ESPN has them. Coverage is reported. Props: the 2024-2025 ESPN open and close.
  3. **Which chain steps does the market agree with?** Regress the closing market margin on the step contributions (plus a constant). A coefficient near 1 means the market prices that step fully, near 0 that it ignores it. Then regress the result minus the closing line on the same contributions: a step the market under-prices should carry a positive coefficient.

## Part E: evaluation

- **Sides:**
  - validation and test margin and total MAE, Brier score of win probability, and ATS record at the validated threshold;
  - each at both points, compared with m13w on the same games (and m15 where stored).
  - **Page rule (M15):** nfle17 replaces the page version only if it improves validation margin MAE AND does not worsen test margin MAE at any prediction point. Otherwise it runs in the background, stored and graded, and the page shows it in the "Engine vs market" section and the engine season record.
- **Props:**
  - MAE, RMSE and Brier at the line, against nflp15 on the same player-games: 2020-2022 by synthetic lines, 2024-2025 at ESPN lines;
  - a betting backtest at the 2024-chosen thresholds on 2025, scored once.
  - Markets that clear it can be flagged (positive 2025 ROI, as the M15 flag rule); the others are "research only".

## Part F: fantasy data

| Source | Use | Rule |
|---|---|---|
| nflverse/ffverse `ff_opportunity` (expected fantasy points, expected targets and carries per player-week) | benchmark, and tested as an input | an input only if it lowers validation (2020-2022) error of the player step it feeds |
| nflverse `ff_rankings` (FantasyPros expert consensus via DynastyProcess) | benchmark only | never an input |
| Sleeper public API (`api.sleeper.app`) projections | benchmark; input only as above | read-only, no key; terms checked and quoted |
| ESPN fantasy projections | benchmark; input only as above | unofficial, keyless; terms checked and quoted |

For each source, reachability from GitHub Actions and the terms of use are verified and recorded first. A source whose terms forbid this use is dropped. As-of: a weekly projection is known at its publish time; a source without timestamps is benchmark-only. Benchmarks compare season MAE of targets, carries, receptions, and receiving and rushing yards with the engine's medians on the same player-weeks.

## Part G: page

- **Engine vs market:** each NFL game gets this section with:
  - the engine margin and total with their 10th-90th range;
  - the market line and the gap;
  - P(cover) at the current line;
  - the top three chain-step contributions;
  - a "flag" only when the gap clears the validated threshold.

  Each player with a stored prop line shows the engine median, line and P(over). It is labelled "nfle17 engine (market-free)".
- **Season record:** an engine season record sits beside the current one, with the M15.1 counting rules: one pick per game and market, the last prediction before kickoff, counted whenever the edge cleared the threshold.

## M17 A1 results: stability (`reports/nfl/stability.csv`, `betdata nfl engine stability`)

Built from the engine's own tables (`nfl/engine/plays.py`: drives, team-games and player-games from play-by-play 2016-2026, plus participation 2016-2025). Training seasons 2016-2019 are shown below; the 2016-2025 columns in the file agree in ranking.

**Two decisions made here, before any engine result:**
- **Neutral plays:** "neutral" is score within 10 points, first or second down, outside the last two minutes of a half, regulation.
- **No line-derived inputs:** nflverse's `xpass` and `pass_oe` are never read, because nflfastR's expected-pass model includes `vegas_wp`, which is built from the betting line. Nor are `wp` and `vegas_wp`. The engine fits its own pass-probability curve from game state on 2016-2019 (step 2).

| Level | Rate | League | Split-half r | Padding | Season-to-season r |
|---|---|---|---|---|---|
| offence | neutral pass rate | 0.515 | 0.64 | 148 plays | 0.31 |
| offence | neutral seconds per play | 32.9 | 0.59 | 124 gaps | 0.23 |
| offence | sack rate | 0.061 | 0.43 | 425 dropbacks | 0.34 |
| offence | pressure rate | 0.274 | 0.73 | 110 dropbacks | 0.25 |
| offence | EPA per dropback | 0.043 | 0.57 | 242 | 0.39 |
| offence | EPA per designed run | −0.081 | 0.24 | 635 | 0.22 |
| offence | TD per drive | 0.211 | 0.62 | 60 drives | 0.35 |
| offence | turnover per drive | 0.102 | 0.18 | 425 drives | 0.29 |
| defence | sack rate allowed | 0.061 | **0.08** | 3,939 | 0.24 |
| defence | pressure rate | 0.274 | 0.50 | 293 | 0.26 |
| defence | EPA per dropback allowed | 0.043 | 0.33 | 655 | 0.31 |
| defence | seconds per play allowed | 32.9 | **−0.01** | none | 0.15 |
| receiver | target share | 0.098 | **0.91** | 23 targets | 0.81 |
| receiver | targets per route (real routes) | 0.178 | 0.65 | 93 routes | 0.68 |
| receiver | targets per proxy route (snap share × dropbacks) | 0.180 | **0.75** | 55 | 0.76 |
| receiver | catch rate | 0.646 | 0.50 | 39 targets | 0.59 |
| receiver | yards per target | 7.37 | **0.26** | 108 targets | 0.37 |
| receiver | aDOT | 8.33 | 0.89 | 5 targets | 0.92 |
| receiver | YAC per reception | 5.17 | 0.54 | 22 | 0.68 |
| receiver | receiving TD per target | 0.045 | **0.13** | 269 | 0.14 |
| rusher | carry share | 0.281 | 0.95 | 7 carries | 0.81 |
| rusher | yards per carry | 4.51 | 0.47 | 94 carries | 0.38 |
| rusher | rushing TD per carry | 0.034 | 0.25 | 247 | 0.42 |
| QB | sack rate | 0.064 | 0.42 | 359 | 0.44 |
| QB | scramble rate | 0.039 | 0.80 | 68 | 0.75 |
| QB | EPA per dropback | 0.025 | 0.41 | 382 | 0.32 |
| QB | interception rate | 0.023 | 0.22 | 853 | 0.29 |

**What it means for the engine** (each used as stated in Part B):
- **Volume is stable and efficiency is noise.** Shares (targets, carries, routes, red zone) need a few games. Yards per target, yards per carry, TD rates and interceptions need hundreds of opportunities, so the engine shrinks them hard to the position or league mean.
- **Defences barely control sacks but do control pressure** (0.08 vs 0.50). So the sack step uses the offence's and QB's sack rates plus the defence's *pressure* rate, as A3 decides.
- **Pace is an offensive trait only.** A defence's seconds per play allowed has a split-half r of −0.01, so the defensive pace term is dropped.
- **Proxy routes are steadier than real routes** for targets per route (0.75 vs 0.65). A2 tests which predicts next-game targets better.
- **Season to season, team rates regress a lot** (r 0.2-0.4). The previous season's weight in a team's prior comes from these numbers, and is cut further where A4 shows a coordinator change matters.

## M17 A3 results: pressure flags (`reports/nfl/m17_checks.json`, `betdata nfl engine checks`)

Participation's `was_pressure` and PFR's `times_pressured` (summed over a team's QBs) were compared per team-game, 2018-2025 (4,422 team-games):

| | Participation | PFR |
|---|---|---|
| Pressure rate per dropback, 2018 → 2025 | 0.279 → 0.297 (2023-2025 higher) | 0.241 → 0.216 (falling) |
| Offence split-half r (padding) | 0.62 (202) | 0.55 (276) |
| Defence split-half r (padding) | 0.42 (461) | 0.26 (923) |

They agree poorly: team-game r = **0.41**, team-season r = 0.47. They also drift apart in level from 2023, with participation rising and PFR falling.

**Decision, by the fixed rule** (agreement below 0.7): pressure is **left out**. The sack step uses the offence's and QB's sack rates and the defence's sack rate allowed, each shrunk with its A1 padding (the defence's is shrunk almost to the league mean, padding 3,939). The two pressure flags are counted differently: participation from NGS tracking, PFR by charters. The live source (PFR) is the less reliable of the two.

## M17 A2 results: routes vs the proxy (validation 2020-2022)

Next-game targets of WR, TE and RB (15,545 player-games with at least two earlier games), predicted from earlier games only:
- exponentially weighted with a half-life of 4 games, and last season's games at weight 0.5;
- shrunk with the A1 paddings;
- the team's prior average volume in each case.

| Target model | MAE | RMSE | Bias |
|---|---|---|---|
| real routes × targets per route (participation) | **1.748** | **2.325** | +0.10 |
| target share × team attempts | 1.750 | 2.330 | +0.06 |
| proxy routes (snap share × team dropbacks) × targets per proxy route | 1.873 | 2.558 | +0.23 |

- **The proxy costs 0.125 targets of MAE (7%).** Snap share counts run-blocking snaps as routes.
- **Target share does as well as real routes** (+0.002) and needs no participation data.
- **Decision:** step 4 uses **target share** (shrunk, padding 23 targets), not routes. This is not a timing workaround: it is as accurate on validation and available in season. Route share stays a candidate adjustment in B3's budget.

## M17 engine: build and validation (2020-2022), logged before any test season was scored

**Engine** (`nfl/engine/`):
- **Rates:** `rates.py`, as-of, shrunk with the A1 paddings toward the era's league mean.
- **Drive model:** `drives.py`, fitted on 2016-2019.
- **Simulation:** `sim.py`.
- **Inputs:** `inputs.py`, from M12's play probabilities, filtered by the depth chart known at the point.

**Two bugs found and fixed during the full build** (not counted as adjustments):
- **QB order.** M12's `role` is a share on a player's previous team. In 2020, Dalton (1.00 from Cincinnati) outranked Prescott (0.99) at Dallas. QBs are now ordered by the depth chart known at the point, and candidates must be on that chart or have played for the team in its last three games.
- **Yard split.** Splitting each drive's yards by expectation biased yards toward runs, because incompletions cluster on failed drives. The split is now made once per team-game.

**Calibration** (training seasons only; 2018-2019 for selection, the last two fit seasons for the test and live fits): three constants, fitted by simulation.
- template time scale 1.04 (drive time of possession leaves out kicks);
- TD intercept (points per team-game);
- pass intercept (dropback share).

**Selection** (2,000 simulations a game, both points, 1,676 game-points; m13w's model-only validation MAE is 9.819):

| Variant | Margin MAE | Kept |
|---|---|---|
| Full build (half-life 6 games, last season ×0.5, paddings ×1, QB weight 0.5) | 10.063 | baseline |
| 1. Longer memory: half-life 10, last season ×0.7 | **10.049** | yes |
| 2. Less shrinkage: paddings ×0.5 | 10.061 | no |
| 3. QB weight 1.0 | 10.062 | no |

**Final design:** half-life 10, last season ×0.7, paddings ×1, QB weight 0.5.

**Validation results of the final design:**
- **Sides:**
  - total MAE 11.04; Brier score 0.223; margin correlation 0.38;
  - volume bias within 5% (plays +1.2%, dropbacks +2.2%, points +3.5%; the 2020 scoring boom carried into 2021-2022).
- **Side threshold** (Part D rule; prekick prediction against the nflverse closing line, best ROI at −110 with at least 100 picks):

| Edge ≥ | 1.0 | 1.5 | 2.0 | 2.5 | 3.0 | **4.0** |
|---|---|---|---|---|---|---|
| Picks | 623 | 511 | 408 | 340 | 265 | **164** |
| ROI | −2.4% | +3.2% | +4.2% | +3.4% | +7.1% | **+11.5%** (95%: −3.1% to +26.0%) |

  The chosen threshold is 4.0 points. Its range includes zero, so no edge is claimed before the test. Moneyline and totals have no engine threshold, so the engine flags spreads only.
- **Test 1 on validation** (do gaps predict results?): the cover rate rises with the gap, weakly. The logistic slope is +0.034 per point (se 0.021); the cover rate at gaps of 4-6 points is 0.62 (n = 100).
- **Props vs nflp15** (validation, nflp15's populations, the same player-games; MAE, TD Brier):

| Market | n | Engine (median) | nflp15 |
|---|---|---|---|
| receiving yards | 19,712 | **21.30** | 21.76 |
| rushing yards | 8,700 | **21.86** | 21.99 |
| receptions | 19,712 | 1.641 | **1.637** |
| passing yards | 3,345 | 70.2 | **65.2** |
| anytime TD (Brier) | 20,197 | 0.1848 | **0.1822** |

  Passing yards lose most. The engine predicts 0 when another QB starts (QB identity is a draw), and when a backup does play it gets the full team volume rather than a share.

**ESPN line history** (`betdata nfl engine fetch lines`, per book; games matched to nflverse):

| Season | NFL games | Close lines | Open lines |
|---|---|---|---|
| 2016-2022 | 267-285 a season | 267-286 (every game) | **none** |
| 2023 | 285 | 285 | 192 (ESPN BET/Caesars, from mid-season) |
| 2024 | 285 | 284 | 284 |
| 2025 | 285 | 285 | 285 |

ESPN keeps no opening lines before late 2023, and offers no separate "opening" pseudo-book. Test 2 (line movement toward the engine) is therefore possible only on 2023-2025, which are test seasons. ESPN also lists projection sites (consensus, numberfire, teamrankings, accuscore) beside the books; they are excluded.
