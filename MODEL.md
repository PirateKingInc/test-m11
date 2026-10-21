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

Markets: passing yards, rushing yards, receiving yards, receptions, and anytime touchdown (a probability). The model is built per player-game at the M12 points.

1. **Role:** snap share, target share, air-yards share, carry share, red-zone targets and carries, from the last 6 games and the season. Route participation comes from participation `route`, 2016-2025; 2026 has none, so it falls back to snap share.
2. **Game script:** team pass attempts and rush attempts from the frozen m13w spread and total.
3. **Teammate injuries:** targets and carries of players out (weighted by 1 − play probability) are reassigned by share.
4. **Opponent:** yards allowed per target and per carry by position, plus FTN man/zone rate (2022+) vs the player's man/zone splits, regressed strongly.
5. **Weather:** wind and precipitation from the M13 weather data, for passing and receiving yards.

- **Distributions:** normal on yards (floored at 0, with a point mass at 0 for receivers), negative binomial on receptions, Poisson on TDs.
- **No backtest:** validation (2020-2022) has no lines, so every NFL market is "research only, not backtested".
- **Test comparison:** test-season error vs the line is reported for 2024-2025.

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
