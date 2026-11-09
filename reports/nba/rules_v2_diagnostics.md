# Rules v2 diagnostics (reporting only)

Owner decision 2026-11-09, Part 3. Nothing here changes a rule or a model. Rules v2 were chosen from the 2026 live ledger; every check on past seasons is labelled as a check on rules chosen from 2026 data.

## Summary

- Live assists unders: with 2+ key teammates out 6-9, -1.9u; when the player played 5+ minutes above projection 7-15, -6.7u.
- Live rebounds unders: with 2+ key teammates out 5-15, -9.1u; when the player played 5+ minutes above projection 6-19, -12.5u.
- Live unders home 96-101, +6.2u, away 87-126, -26.6u.
- Next Gen 8+ points from the close: its side hit 50.3% (engine 62.7%, close 49.1%).
- League rebounding this season: 44.15 per team-game (past 43.39-45.19); per missed shot 0.878 (past 0.8847-0.8968); offensive share 0.275 (past 0.2208-0.2602).
- Rules a-c replayed on 2022-23 to 2025-26 opening lines (a check on rules chosen from 2026 data; 2022-23 is in-sample for nbap15): filtered: rebounds: 1874-1894, +300.4u (ROI 0.08); filtered: teammates out (ex post): 799-974, -25.2u (ROI -0.014); filtered: under 5.5+ assists: 460-341, +103.6u (ROI 0.129); kept: 1234-1590, +8.0u (ROI 0.003). In those seasons rules (a) and (b) would have removed profitable bets; only (c) removed losing ones.

Sources: live_and_league: GitHub runner, restored hourly state, 2026-11-09 (probe-env run 42704500680); backtest: props backtest (opening lines 2022-23 to 2025-26; nbap15 fitted on 2015-2022, so 2022 is in-sample) on the M20 working database copy of 2026-11-03

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

## 1a. League rebounding by season

| Season | Team-games | Reb / team-game | Reb / missed shot | OREB share | FGA |
|---|---|---|---|---|---|
| 2015 | 2632 | 43.71 | 0.8946 | 0.2384 | 84.48 |
| 2016 | 2616 | 43.39 | 0.893 | 0.2329 | 85.28 |
| 2017 | 2622 | 43.47 | 0.8937 | 0.2227 | 85.95 |
| 2018 | 2622 | 45.19 | 0.8935 | 0.2288 | 89.14 |
| 2019 | 1954 | 44.83 | 0.8917 | 0.2255 | 88.8 |
| 2020 | 2674 | 44.01 | 0.8968 | 0.2208 | 87.75 |
| 2021 | 2646 | 44.29 | 0.8944 | 0.2319 | 87.8 |
| 2022 | 2640 | 43.42 | 0.8931 | 0.2402 | 88.23 |
| 2023 | 2638 | 43.44 | 0.8917 | 0.2423 | 88.62 |
| 2024 | 2642 | 44.02 | 0.8873 | 0.2524 | 89.0 |
| 2025 | 2644 | 43.67 | 0.8847 | 0.2602 | 88.82 |
| 2026 | 292 | 44.15 | 0.878 | 0.275 | 88.1 |

## 1b. Projection error (actual minus projected) by player type

- live rebounds: big/bench n=217 err +0.28 (MAE 2.66, min err -0.92); big/starter n=868 err +0.59 (MAE 2.83, min err 0.44); guard/bench n=219 err -0.54 (MAE 1.57, min err -2.33); guard/starter n=1125 err +0.07 (MAE 1.82, min err 0.47); wing/bench n=674 err -0.03 (MAE 1.76, min err 0.16); wing/starter n=1714 err -0.08 (MAE 2.03, min err 0.01)
- live assists: big/bench n=48 err +0.05 (MAE 1.73, min err -0.87); big/starter n=599 err -0.05 (MAE 1.65, min err 0.25); guard/bench n=278 err -0.18 (MAE 1.86, min err -2.48); guard/starter n=1129 err +0.19 (MAE 2.19, min err 0.46); wing/bench n=296 err -0.27 (MAE 1.19, min err 0.87); wing/starter n=1305 err -0.22 (MAE 1.48, min err 0.07)
- live points: big/bench n=222 err -1.22 (MAE 4.23, min err -1.01); big/starter n=867 err -0.43 (MAE 5.21, min err 0.43); guard/bench n=291 err -1.82 (MAE 4.88, min err -2.52); guard/starter n=1133 err -0.07 (MAE 5.8, min err 0.46); wing/bench n=872 err -0.8 (MAE 5.12, min err 0.17); wing/starter n=1761 err -0.95 (MAE 5.29, min err 0.05)
- past rebounds: 2022 big/bench n=1514 err +0.19; 2022 big/starter n=2474 err +0.21; 2022 guard/bench n=1106 err +0.02; 2022 guard/starter n=3101 err +0.05; 2022 wing/bench n=4510 err -0.04; 2022 wing/starter n=6036 err +0.06; 2023 big/bench n=1410 err +0.13; 2023 big/starter n=2665 err +0.32; 2023 guard/bench n=1381 err -0.1; 2023 guard/starter n=3941 err +0.05; 2023 wing/bench n=5116 err -0.03; 2023 wing/starter n=5333 err +0.05; 2024 big/bench n=663 err -0.01; 2024 big/starter n=1327 err +0.16; 2024 guard/bench n=584 err -0.04; 2024 guard/starter n=1840 err +0.05; 2024 wing/bench n=2943 err -0.09; 2024 wing/starter n=2882 err +0.03; 2025 big/bench n=345 err -0.19; 2025 big/starter n=703 err +0.2; 2025 guard/bench n=240 err +0.12; 2025 guard/starter n=895 err +0.13; 2025 wing/bench n=970 err -0.19; 2025 wing/starter n=1895 err +0.0
- past assists: 2022 big/bench n=1245 err -0.01; 2022 big/starter n=2364 err +0.03; 2022 guard/bench n=1120 err +0.07; 2022 guard/starter n=3104 err +0.18; 2022 wing/bench n=4020 err -0.04; 2022 wing/starter n=5720 err -0.04; 2023 big/bench n=1210 err -0.0; 2023 big/starter n=2564 err +0.08; 2023 guard/bench n=1395 err -0.05; 2023 guard/starter n=3938 err +0.16; 2023 wing/bench n=4701 err -0.05; 2023 wing/starter n=5074 err +0.04; 2024 big/bench n=594 err -0.09; 2024 big/starter n=1214 err -0.0; 2024 guard/bench n=576 err -0.06; 2024 guard/starter n=1782 err -0.08; 2024 wing/bench n=2664 err -0.05; 2024 wing/starter n=2705 err -0.05; 2025 big/bench n=71 err -0.17; 2025 big/starter n=548 err +0.11; 2025 guard/bench n=284 err +0.15; 2025 guard/starter n=911 err +0.2; 2025 wing/bench n=533 err -0.27; 2025 wing/starter n=1406 err -0.14

## 1c. The M16 audit's rebound backtest (opening lines)

{"bets": {"bets": 1991, "w": 1011, "l": 980, "p": 0, "win_rate": 0.5078, "win_rate_95": [0.4858, 0.5297], "roi": 0.1057}, "by_season": {"2023": {"bets": 998, "w": 534, "l": 464, "p": 0, "win_rate": 0.5351, "win_rate_95": [0.504, 0.5658], "roi": 0.1345}, "2024": {"bets": 759, "w": 351, "l": 408, "p": 0, "win_rate": 0.4625, "win_rate_95": [0.4273, 0.498], "roi": 0.0556}, "2025": {"bets": 234, "w": 126, "l": 108, "p": 0, "win_rate": 0.5385, "win_rate_95": [0.4745, 0.6012], "roi": 0.1451}}}

## 3. Primary ballhandlers: assist projection error

- 2022 leader: n=1629 err +0.23 (MAE 2.35), over rate 52.2%
- 2022 others: n=15944 err -0.0 (MAE 1.41), over rate 48.5%
- 2023 leader: n=1487 err +0.27 (MAE 2.22), over rate 50.2%
- 2023 others: n=17395 err +0.02 (MAE 1.47), over rate 48.9%
- 2024 leader: n=729 err +0.08 (MAE 2.31), over rate 48.3%
- 2024 others: n=8806 err -0.06 (MAE 1.45), over rate 49.0%
- 2025 leader: n=376 err -0.02 (MAE 2.33), over rate 48.9%
- 2025 others: n=3377 err -0.02 (MAE 1.61), over rate 46.9%

## 4 (past). Home vs away unders in the props backtest

- 2022 away: 755-739, +123.2u (ROI 0.082)
- 2022 home: 775-932, -45.8u (ROI -0.027)
- 2023 away: 513-426, +150.4u (ROI 0.16)
- 2023 home: 537-505, +89.5u (ROI 0.086)
- 2024 away: 259-289, +35.6u (ROI 0.065)
- 2024 home: 322-353, +46.1u (ROI 0.068)
- 2025 away: 109-120, +4.2u (ROI 0.018)
- 2025 home: 111-124, +3.6u (ROI 0.015)

## 6. Rules a-c on past seasons (a check on rules chosen from 2026 data)

Rule (c) uses an ex-post stand-in (key teammates who did not play).

- 2022 all backtested rebound+assist bets: 1924-2244, +19.4u (ROI 0.005)
- 2023 all backtested rebound+assist bets: 1382-1364, +247.9u (ROI 0.09)
- 2024 all backtested rebound+assist bets: 755-880, +79.0u (ROI 0.048)
- 2025 all backtested rebound+assist bets: 306-311, +40.4u (ROI 0.065)
- 2022 filtered: rebounds: 869-922, +87.9u (ROI 0.049)
- 2022 filtered: teammates out (ex post): 374-441, -3.1u (ROI -0.004)
- 2022 filtered: under 5.5+ assists: 192-185, +3.1u (ROI 0.008)
- 2022 kept: 489-696, -68.4u (ROI -0.058)
- 2023 filtered: rebounds: 533-461, +135.3u (ROI 0.136)
- 2023 filtered: teammates out (ex post): 240-307, -18.1u (ROI -0.033)
- 2023 filtered: under 5.5+ assists: 204-89, +101.4u (ROI 0.346)
- 2023 kept: 405-507, +29.2u (ROI 0.032)
- 2024 filtered: rebounds: 349-401, +48.3u (ROI 0.064)
- 2024 filtered: teammates out (ex post): 137-166, -2.3u (ROI -0.008)
- 2024 filtered: under 5.5+ assists: 42-42, +3.8u (ROI 0.045)
- 2024 kept: 227-271, +29.2u (ROI 0.059)
- 2025 filtered: rebounds: 123-110, +28.9u (ROI 0.124)
- 2025 filtered: teammates out (ex post): 48-60, -1.8u (ROI -0.016)
- 2025 filtered: under 5.5+ assists: 22-25, -4.6u (ROI -0.098)
- 2025 kept: 113-116, +17.9u (ROI 0.078)
