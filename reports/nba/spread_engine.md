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
