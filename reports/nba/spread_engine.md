# Spread engine (2026-11-14)

Owner decision 2026-11-14 (MODEL.md "Spread engine"). Report only unless the D4 bar is met. The engine's constants are fitted on 2015-2022 only; every spread-model fit uses only seasons before the one it scores; T was chosen on walk-forward validation (2019-2022, closing lines) and recorded in MODEL.md before the test was run.

## D1: why the engine's game probabilities are squeezed (2016-2022)

- Games: 8803.
- The engine's margin is shrunk: actual margin on the engine's margin, slope **1.149** (1 = right scale); the engine's margin on the market's, slope **0.498** (the market's own: 0.947); spread of the engine's margins 4.3 pts vs the market's 6.55.
- Home court: mean margin engine +2.05, actual +2.33, market +2.36.
- Margin to probability: the simulations' margin spread 16.03 pts per game vs the actual residual 13.46 pts, so even a right mean would be pulled toward 50/50.
- The moneyline logit slope on the market's: raw **0.357**; mean fixed 0.412; spread fixed 0.427; both fixed **0.494**.
- D2 fit on 2016-2022: margin = -0.02 + 1.15 x engine margin, sigma 13.44 pts; engine-vs-market slope after the fix 0.572.

## D3 threshold (pre-registered): T = 0.58

| T | Validation underdog picks (walk-forward 2019-2022, closing line) |
|---|---|
| 0.52 | 1661-1662-42 (50.0%, 95% 48.3-51.7), -152.0u at -110 |
| 0.53 | 1587-1569-41 (50.3%, 95% 48.5-52.0), -126.3u at -110 |
| 0.54 | 1497-1486-37 (50.2%, 95% 48.4-52.0), -125.1u at -110 |
| 0.55 | 1420-1398-33 (50.4%, 95% 48.5-52.2), -107.1u at -110 |
| 0.56 | 1326-1298-30 (50.5%, 95% 48.6-52.4), -92.5u at -110 |
| 0.57 | 1253-1205-29 (51.0%, 95% 49.0-52.9), -65.9u at -110 |
| 0.58 | 1164-1119-26 (51.0%, 95% 48.9-53.0), -60.8u at -110 |
| 0.59 | 1066-1025-26 (51.0%, 95% 48.8-53.1), -55.9u at -110 |
| 0.60 | 966-940-25 (50.7%, 95% 48.4-52.9), -61.8u at -110 |
| 0.61 | 873-849-23 (50.7%, 95% 48.3-53.0), -55.4u at -110 |
| 0.62 | 784-759-19 (50.8%, 95% 48.3-53.3), -46.3u at -110 |

## D3 test: 2023-24 to 2025-26 (opening lines; fitted once on 2016-2022)

- Brier of P(home covers): model 0.26009 vs the line (no-vig 0.5) 0.25 (3912 games, pushes dropped).
- Underdogs at T: 761-753-5 (50.3%, 95% 47.8-52.8), -61.2u at -110.
- All sides at T: 954-934-6 (50.5%, 95% 48.3-52.8), -66.7u at -110.
- Engine-vs-market slope (fixed margin on the market's): 0.701.

| Season | Underdogs | All sides |
|---|---|---|
| 2023-24 | 217-248-5 (46.7%, 95% 42.2-51.2), -50.7u at -110 | 287-305-6 (48.5%, 95% 44.5-52.5), -44.1u at -110 |
| 2024-25 | 248-214 (53.7%, 95% 49.1-58.2), +11.4u at -110 | 317-288 (52.4%, 95% 48.4-56.4), +0.2u at -110 |
| 2025-26 | 296-291 (50.4%, 95% 46.4-54.4), -21.9u at -110 | 350-341 (50.6%, 95% 46.9-54.4), -22.8u at -110 |

Calibration (P(home covers) bins vs actual cover rate):

| Bin | Games | Mean P | Covered |
|---|---|---|---|
| 0.0-0.1 | 1 | 0.088 | 0.000 |
| 0.1-0.2 | 12 | 0.164 | 0.333 |
| 0.2-0.3 | 101 | 0.265 | 0.366 |
| 0.3-0.4 | 549 | 0.360 | 0.508 |
| 0.4-0.5 | 1181 | 0.454 | 0.495 |
| 0.5-0.6 | 1246 | 0.547 | 0.501 |
| 0.6-0.7 | 641 | 0.640 | 0.496 |
| 0.7-0.8 | 148 | 0.737 | 0.554 |
| 0.8-0.9 | 30 | 0.840 | 0.567 |
| 0.9-1.0 | 3 | 0.916 | 0.667 |

## D4: the bar (test seasons, underdogs only, at T)

**NOT MET: nothing ships live**

- picks >= 300: pass (1519)
- win >= 53.5%: FAIL (0.5026)
- 95% low > 50%: FAIL ([0.4775, 0.5278])
- positive in >= 2 of 3 seasons: FAIL (1)
- engine-vs-market slope 0.8-1.25: FAIL (0.701)

**By how much it missed:**

- Win rate 50.3% vs 53.5%: -3.2 pts.
- 95% range low end 47.8% vs above 50%: -2.3 pts.
- Positive seasons 1 of 3 vs at least 2.
- Engine-vs-market slope 0.701 vs 0.8-1.25: -0.099 below the floor.

**What the next fix would be.** The D2 rescaling cannot create information the engine's margin does not carry: after de-shrinking (slope 1.15) and the right residual spread (13.4 pts, not the simulations' 16), the engine's margin still moves only ~0.5-0.7 times as much as the market's, and the cover rate is ~50% in every probability bin from 0.3 to 0.7. The squeeze is mostly missing signal in the engine's team strength, not its probability step. Next: (1) feed the engine's game margin from team-level ratings that track the market (the M13/M14 team models' margin, or a player-rating prior shrunk less toward the mean, checked on 2016-2022 by the engine-on-market slope first); (2) re-run this same pre-registered test only when validation shows the engine-on-market slope >= 0.8 and validation underdog picks at or above 53.5%; (3) never fit on 2023-2025.

## D5: would the fix help Next Gen's moneyline probability? (recommendation only)

- Test seasons, 3937 games: Brier raw engine 0.21571, fixed 0.21186, closing market 0.19944; log loss 0.62139 / 0.6121 / 0.58218; logit slope on the market's raw 0.448, fixed 0.634.

## Live season (labelled)

- The squeeze, reproduced on engine_grades.csv (raw live engine, 68 games): engine logit = 0.069 + 0.405 x market logit. After the D2 fix the moneyline logit slope on the test seasons rises from the raw engine's to the fixed one's (D5); the fixed model needs the engine's mean margin, stored live only from Nov 15.
- Raw engine (before the fix), home-side closing spreads in engine_grades.csv: 68 games; the raw engine takes the underdog at P >= 0.58 in 37 of 68: 19-18 (51.3%, 95% 35.9-66.5), -0.7u at -110.
- The fixed spread model's live inputs (the engine's mean margin, both sides, the morning line) are stored from the Nov 15 morning run (engine_game_grades.csv); its live record starts then.
