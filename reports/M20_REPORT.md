# M20 report: one NBA engine, a success test that counts the margin

Written 2026-11-03. MODEL.md "M20" holds the design (written before any result) and the results log; this report summarises them. **Unchanged:** the card pipeline (still the existing NBA picks), nba15, nbap15, F, C-NBA, C-NFL, every NFL model, every stored pick, result and grade, and the main page (a structure test enforces it).

## The state summary M20 started from (issue #290)

1. Hourly runner (Actions + Pages, clock-dispatched, 7am-10pm ET) plus a poller job; state in the Actions cache; ledger CSVs committed hourly, append-only.
2. NBA had two disconnected models: `nba15` sides and `nbap15` props.
3. NFL: m13w on the page, m15 / nfle17 / nflpe17 in the background (untouched).
4. Since M18 the de-vigged consensus is the answer key; signals C-NBA, C-NFL, F, G1, G2, sharp-nba; gate = mean no-vig CLV > 0 over 200.
5. M19: the official record is the daily card only; the Research tab holds signal records; the main page layout was fixed (frozen for M20).
6. Ledger: 469 picks (F 329, G2 100, G1 18, C-NBA 14, m16-legacy 6, C-NFL 1, sharp-nba 1).
7. F crossed 200 graded at 2026-11-03 12:00Z, a few hours before the new rule was written (so the rule is not pre-registered for F).
8. F: 47.8% wins, fair 52.0% vs consensus close 44.9%, CLV +1.6 pp, EV at close −3.2%.
9. EV at close negative for every signal with volume; unders dominate F.
10. Data: sportsdataverse box scores/play-by-play 2015-16 on, injury reports from 2018-12-17, ESPN closing lines (openers 2015-17, 2023 on), ESPN opening prop lines 2022-23 on.

## 1. Part A: the success test that counts the margin (#292, PR #313)

A signal works only if all three checks pass over ≥ 200 graded picks: calibration (gap not significantly above 0, ECE ≤ 0.05), a Brier score closer than the closing no-vig line, and a positive EV at the close (closing no-vig × odds taken − 1), each with a 95% range. CLV is split into market movement and book difference. `betdata alerts checks`; the Research tab shows each signal's line.

| Signal | Graded | Calibration | Brier vs close | EV at close | CLV (move + book) | Verdict |
|---|---|---|---|---|---|---|
| F | 314 | ECE 0.042, gap +4.2 pp (−1.3 to +9.8) pass | +0.0049 (−0.0059 to +0.0157) fail | −3.2% (−4.8 to −1.6) fail | +1.6 = +1.2 + 0.4 | **does not work** (passed the old CLV gate) |
| G2 (retired) | 100 | ECE 0.090 fail | +0.0073 fail | −3.7% fail | +2.1 = −0.3 + 2.4 | too few, failing all |
| G1 (retired) | 18 | no fair probability | – | −4.1% fail | +0.1 | too few |
| C-NBA | 14 | – | – | −0.9% (−3.1 to +1.2) | +1.7 = +1.1 + 0.6 | too few |
| C-NFL, sharp-nba | 1 each | – | – | – | – | too few |

G1 and G2 are retired (#293): the poller makes no new picks; history, grading and records stay.

## 2. Stability table highlights (B2, #294)

Volume is stable, shooting is not: usage padding 80 possessions (season-to-season r 0.86), 3PA rate 62, assists 98; 2P% / 3P% need 139 / 129 attempts and carry little across seasons (r 0.31 / 0.38); FT% 47 attempts. Teams: pace 3.3 games; 3P% allowed 3,131 attempts (split-half 0.29: mostly luck); DREB rate 1,007 chances. Full table: `reports/nba/m20_stability.csv`.

## 3. Competitive minutes, outliers, role changes (B3-B6)

- **Competitive minutes:** CM1 (|margin| ≥ 15 with ≤ 5:00 left) won: 5.2571 minutes MAE vs CM2 (win prob > 95%) 5.2663 and CM3 (≥ 20 in Q4) 5.2568, a tie that goes to the simpler CM1.
- **Outliers:** down-weighting blowouts, overtime, foul trouble or early exits never helped (blowout weight 0 → 5.270, 1 → 5.125 at h = 3). The baseline is a share of competitive time, so blowouts are already neutralised; weights stay 1. The mechanism is tested (a blowout is down-weighted when its weight is below 1), but validation set it off.
- **Role changes:** a 3-game minutes half-life wins for everyone (5.125 vs 5.257 at 10 games); role-change detection adds nothing on top. A Schröder-like trade into a bigger role projects more than 3 minutes above the old role (fixture).
- **Team assembly adjustments (3 of 3):** era-level player priors (kept: totals 15.90 → 15.31), a strength stretch (dropped: worse at every value), era-level team priors (kept: 15.29).

## 4. Add-ons (Part D): none kept

| Add-on | Effect on its target (validation) | Decision |
|---|---|---|
| D1 with/without key teammates | assists 1.413 → 1.508 on affected rows (adjusted version 1.419 → 1.513) | dropped |
| D2 rebounding environment | rebounds 1.9227 → 1.9231 | dropped |
| D3 assists × teammate shooting | assists −0.01% | dropped |
| D4 script into minutes (removal) | minutes +0.15% without it, points better | removed |

## 5. Engine vs nba15, nbap15 and the closing line

**Validation (2021-23):**
| | Engine | nba15 | Close |
|---|---|---|---|
| Margin MAE | 10.87 | 10.46 | 10.26 |
| Total MAE | 15.10 | 14.70 | 14.15 |
| Moneyline Brier | 0.2287 | – | 0.2137 |
| Props Brier (pts/reb/ast/3PM) vs opening no-vig | 0.2645 / 0.2584 / 0.2518 / 0.2515 | 0.2495 / 0.2481 / 0.2470 / 0.2472 | 0.2500 / 0.2472 / 0.2433 / 0.2436 |

Blend weights learned on validation: spreads 0, totals 0, moneyline 0.1, points 0.1, rebounds 0.2, assists 0.2, threes 0.1, PRA 0; Platt on points (0.51) and assists (1.14); θ 5% EV (game), 10% (props).

**Test, scored once (2023-26; repeat look for sides):**
| | Engine | nba15 | Close |
|---|---|---|---|
| Margin MAE | 11.37 | 10.87 | 10.77 |
| Total MAE | 14.93 | 14.82 | 14.32 |
| Moneyline Brier | 0.2157 | – | 0.1993 |
| Prop median MAE vs the line (pts / reb / ast / 3PM) | 5.02 / 2.01 / **1.51** / **1.08** | – | 4.91 / 2.01 / 1.54 / 1.15 |

**Three checks on test picks (clean lines, frozen design):**
| Picks | n | ROI | Calibration | Brier vs reference | EV at reference |
|---|---|---|---|---|---|
| Engine props, opening line | 329 | +18.5% | pass (gap −3.0 pp) | pass (−0.014) | fail (by construction) |
| Engine moneyline at the close | 526 | −29.3% | fail | fail | fail (−4.2%) |
| Engine moneyline at the open, vs the close | 439 | −3.3% | pass | fail (+0.006) | **pass (+5.0%, +1.9 to +8.2)** |
| Rebound unders | 49 | +33% | fail | pass | fail — too few |
| Primary-ballhandler assists | 8 | +64% | fail | pass | fail — too few |
| nbap15 props (its thresholds) | 1,842-7,322 per market | – | fail (gap +5 to +11 pp, overconfident) | mixed | fail (−5.9 to −6.9%) |
| nba15 moneyline at the close | 1,711 | – | fail (+6.8 pp) | fail | fail (−4.1%) |

Primary ballhandlers' assist lines ≥ 5.5 (2,425 test lines): the overs won 45.8% against a no-vig 49.9%; the engine said 44.1%.

**Data bug found in the test:** about 1% of stored ESPN prop lines are not a real market (both sides +1200 to +1600, or another market stored as rebounds), and they made up a third of the first scoring's prop picks (66% "wins"). The same filter was applied to validation and test with the frozen design and both scorings are committed; live quotes get the same guard.

## 6. Live, in the background (Part G, #308)

- **Hourly:** the frozen fit (`config/fits/nba_engine_m20.json`, constants on 2015-16 to 2025-26) predicts the next two days' games, 10,000 simulations each, into `data/derived/nba_engine_live.json`. No hourly refit. Measured on the automatic 19:00 UTC run: run step 118.7 s (84 s the hour before), NBA predict 38.9 s for 14 games.
- **Picks:** the poller prices DK/FD lines at its hourly sweep of each game, on new lines, and after qualifying injury changes (re-simulating the game), and records `nba-engine` research picks: never pushed, never labelled, never on the card or a sport tab.
- **Live proof, two consecutive automatic (clock) hourly runs:**

| Run (UTC) | Actions run | Research tab "NBA engine" section | Screenshot (390 px) |
|---|---|---|---|
| 19:00 | 41699110033 | "Live predictions: 14 games, generated 19:01 UTC" | verify-page 41700172116 → `reports/m20/live_research_nba_engine_1900utc.png` |
| 20:00 | 41708473875 | "Live predictions: 14 games, generated 20:01 UTC" | verify-page 41709313639 → `reports/m20/live_research_nba_engine_2000utc.png` |

  The main page at 390 px (`reports/m20/live_main_390_2000utc.png`, `live_nba_tab_390_2000utc.png`) is the M19 layout. Its structure is pinned by `tests/runner/test_ui_freeze.py` against a snapshot taken before M20.
- **Live engine picks so far: none.** No `nba-engine` pick had been recorded by the 20:00 UTC ledger sync. This is consistent with the validated design: spreads and totals carry zero engine weight, and moneylines and props need ≥ 5% / 10% EV after a 10-20% blend toward the engine (about 0.1-0.2 picks per game on validation). The first picks will appear on the Research tab at the next ledger sync after a sweep finds one.

## 7. SWITCH.md (#309)

How the card would switch to the engine, for the owner to approve, not executed. The precondition is that `nba-engine` works on all three checks over ≥ 200 graded live picks. The steps: add `"engine"` to the label/card signals, with TAKE at EV ≥ 1.5θ; NBA card pool = engine only. To archive: nba15 and nbap15 hourly predictions stopped (fits and history kept), F retired like G1/G2, C-NBA off via its kill switch. Rollback is a revert. The main page structure must not change.

## 8. Verification

a) **Regression:** full suite 695 passed locally (with the engine tests); CI green (sqlite + postgres) on every PR. Ledger append-only tests pass, and no stored pick, result or grade changed. nba15 rebuilt from today's published files reproduces its recorded test MAE within 0.015 (10.870 vs 10.863 pretip; validation 10.473 vs 10.468). It is close but not bit-exact, because the original runner database was not available here.
b) **UI freeze:** `test_ui_freeze.py` passes. Only the Research tab changed.
c) **As-of harness:** every engine input rebuilt from all data, from rows known by the point, and with later rows scrambled gives identical predictions (`tests/nba/engine/test_core.py`, with the production era flags on).
d) **Sum constraints:** checked in every simulated game. Minutes = 240 + 25·OT per team, team points = Σ players, OREB + opponent DREB = credited misses, assists ≤ FGM, no player above his team. All 2,643 validation and 3,962 test games passed.
e) **Fixtures:** a blowout's minutes are down-weighted when the weight is below 1 (validation chose 1, see §3). A Schröder-like trade into a bigger role projects above the old role. D1 was not kept, so no usage-shift fixture applies.
f) **Three-check tables:** every signal (§1) and the engine on validation and the labelled test (§5, MODEL.md), with 95% ranges.
g) **Live proof:** §6.

## 9. Issues and PRs

- **Issues (all closed):** #290 plan, #291 UI freeze, #292 success test, #293 G1/G2, #294 stability, #295 competitive minutes, #296 outliers, #297 role changes, #298 minutes, #299 team assembly, #300 simulation, #301 blend, #302-#305 D1-D4, #306 pick rule, #307 evaluation, #308 live + Research, #309 SWITCH.md, #310 docs.
- **PRs:** #311 UI freeze, #312 plan, #313 Part A, #314 inputs + stability, #315 states/means, #316 selection, #317 simulation, #318 add-ons/blend/CLI, #319 B7 adjustments + live wiring + SWITCH.md, #320 design + frozen fit, #321 evaluation, plus this report.

## 10. BACKLOG additions

See BACKLOG.md "M20 additions": the engine trails the market (next: competitive-time player stats from play-by-play events, stint-level team strength, a possession-level simulation); separating ejections from injury exits; true on/off D1 from per-stint player stats; F's next 200 picks under the M20 rule; database-rebuild drift; PRA has no validation lines.

## 11. Honest assessment

**The engine does not beat the market; at best it partly matches it, and only after the market is blended in.**
- On its own it is worse than the line in every market: spread and total probabilities carry no information beyond the closing line, and the moneyline and props lose on Brier and log loss on validation and test.
- Its side errors are worse than nba15's (test margin MAE 11.37 vs 10.87) and the close's (10.77).
- The validated blend therefore keeps the market in charge: 0% engine on spreads and totals, 10-20% elsewhere.

What it does do:
- Its prop medians beat the opening line on assists and threes.
- Its prop picks are calibrated (gap −3 pp), where nbap15's are overconfident (+5 to +11 pp, the live problem that started M20).
- It keeps team totals consistent with the players.

Its apparent test edges (props beating the opening no-vig; opening moneylines +5% EV against the close) come from betting stale opening lines with later-in-the-day information. M14 flagged that pattern as not achievable, so it is not evidence of beating the close.

The three-check rule now says plainly what CLV hid: no signal, old or new, has shown profit after the margin. F fails it on 314 picks, despite passing the old CLV gate. The live engine's grading against the M18 consensus close over its first 200 picks is the test that matters. Until it passes all three checks, the card should not switch.
