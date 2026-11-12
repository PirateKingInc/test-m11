# F verdict: the next 200 picks from Nov 4 (2026-11-12)

Pre-registered in BACKLOG.md (M20): "Judge F again on its next 200 picks (from 2026-11-04) under the M20 rule." Run as written (MODEL.md M20 Part A, the three checks that replaced the M18 Part H gate; `betdata/checks.py`). The verdict changes no rule, pick or grade.

**Verdict: F does not work** — failed: calibration, brier vs close, EV at close.

Window: F's first 200 graded picks (win/loss/push; voids, m16-legacy and duplicates excluded) created on or after 2026-11-04 00:00 ET, in grading order: 200 picks, created 2026-11-04T11:00:39+00:00 to 2026-11-08T11:00:30+00:00. Pushes are left out of the checks (MODEL.md). Closing no-vig = book_prob + clv_consensus (the consensus close).

## The verdict window (the pre-registered test)

- Picks: 200 graded (80-120-0; 200 win/loss in the checks), -25.78u at 1u flat, ROI -12.9%
- Mean model probability 51.9% vs win rate 40.0%
- 1. Calibration: ECE 0.129 (max 0.050); gap +11.9 pp; gap 95% range +5.08 to +18.62 pp → **fail**
- 2. Brier vs the no-vig close: +0.0175 (95% +0.0039 to +0.0311) over 198 picks with a close → **fail**
- 3. EV at close: -4.34 (95% -6.59 to -2.09)% over 198 picks with a close → **fail**
- Result: **does not work** (failed: calibration, brier vs close, EV at close)

### Reliability (5-point buckets of the model probability; display only, the ECE uses MODEL.md's bins)

| Bucket | n | Mean predicted | Win rate |
|---|---|---|---|
| 40-45% | 2 | 44.3% | 50.0% |
| 45-50% | 54 | 48.1% | 38.9% |
| 50-55% | 118 | 52.2% | 35.6% |
| 55-60% | 22 | 56.9% | 59.1% |
| 60-65% | 2 | 61.6% | 50.0% |
| 75-80% | 2 | 77.2% | 100.0% |

## NOT the verdict: every F pick graded since Nov 4

314 graded picks (the window is the first 200).

- Picks: 314 graded (137-177-0; 314 win/loss in the checks), -18.49u at 1u flat, ROI -5.9%
- Mean model probability 51.8% vs win rate 43.6%
- 1. Calibration: ECE 0.092 (max 0.050); gap +8.2 pp; gap 95% range +2.72 to +13.61 pp → **fail**
- 2. Brier vs the no-vig close: +0.0133 (95% +0.0018 to +0.0248) over 309 picks with a close → **fail**
- 3. EV at close: -4.11 (95% -6.00 to -2.23)% over 309 picks with a close → **fail**
- Result: **does not work** (failed: calibration, brier vs close, EV at close)

## NOT the verdict: the window, one pick per player per game

200 raw picks → 158 after keeping the highest model probability per player and game (F often alerts the same player at several books or lines; those results are correlated).

- Picks: 158 graded (66-92-0; 158 win/loss in the checks), -14.57u at 1u flat, ROI -9.2%
- Mean model probability 51.9% vs win rate 41.8%
- 1. Calibration: ECE 0.115 (max 0.050); gap +10.1 pp; gap 95% range +2.45 to +17.84 pp → **fail**
- 2. Brier vs the no-vig close: +0.0135 (95% -0.0018 to +0.0288) over 156 picks with a close → **fail**
- 3. EV at close: -4.48 (95% -6.88 to -2.07)% over 156 picks with a close → **fail**
- Result: **does not work** (failed: calibration, brier vs close, EV at close)

