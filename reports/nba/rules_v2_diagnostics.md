# Rules v2 diagnostics (reporting only)

Owner decision 2026-11-09, Part 3. Nothing here changes a rule or a model. Rules v2 were chosen from the 2026 live ledger; every check on past seasons is labelled as a check on rules chosen from 2026 data.

## 2. Role expansion (live OG prop unders)

**assists** — by key teammates Out/Doubtful at pick time: 0: 54-55, +4.5u; 1: 21-10, +13.7u; 2+: 6-9, -1.9u; unknown: 5-5, +0.4u
by minutes vs projection: 5+ above: 7-15, -6.7u; 5+ below: 6-3, +4.1u; unknown: 44-37, +12.0u; within 5: 29-24, +7.2u
minutes 5+ above projection: with 2+ teammates out 33.3% of 6; with 0-1 out 25.7% of 70

**rebounds** — by key teammates Out/Doubtful at pick time: 0: 69-92, -13.3u; 1: 18-31, -10.2u; 2+: 5-15, -9.1u; unknown: 5-10, -4.4u
by minutes vs projection: 5+ above: 6-19, -12.5u; 5+ below: 9-5, +5.7u; unknown: 50-70, -13.9u; within 5: 32-54, -16.4u
minutes 5+ above projection: with 2+ teammates out 40.0% of 5; with 0-1 out 19.3% of 109

## 4. Home vs away unders (live OG props)

- assists: home 46-34, +15.8u; away 40-45, +0.8u
- rebounds: home 50-67, -9.6u; away 47-81, -27.4u
- all: home 96-101, +6.2u; away 87-126, -26.6u

## 5. Next Gen: calibration and accuracy vs the close

- Overall: 1545 graded; Brier engine 0.2514 vs close 0.2450
- assists: 295 graded; Brier engine 0.2479 vs close 0.2521 — no clear difference yet
- h2h: 31 graded; Brier engine 0.1906 vs close 0.1482 — the close beats the engine
- points: 428 graded; Brier engine 0.2572 vs close 0.2485 — no clear difference yet
- rebounds: 398 graded; Brier engine 0.2550 vs close 0.2445 — no clear difference yet
- spreads: 31 graded; Brier engine 0.2660 vs close 0.2499 — no clear difference yet
- threes: 331 graded; Brier engine 0.2467 vs close 0.2431 — no clear difference yet
- totals: 31 graded; Brier engine 0.2581 vs close 0.2495 — no clear difference yet
- Engine vs close gap 0-4 pts: 515 predictions; the engine's side hit 56.9% (engine said 51.6%, close 49.6%)
- Engine vs close gap 4-8 pts: 430 predictions; the engine's side hit 51.2% (engine said 55.6%, close 49.7%)
- Engine vs close gap 8+ pts: 600 predictions; the engine's side hit 50.3% (engine said 62.7%, close 49.1%)
- Bands: 65-70%: 54.9% hit vs 67.4% (162); 70-80%: 64.0% hit vs 73.6% (89); 80%+: 57.1% hit vs 82.1% (7)

Items 1, 3, 4 (past seasons) and 6 need the full database: run `betdata diag rules-v2 --db` on a runner.
