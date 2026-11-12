# Threes under today's OG rules (research; hindsight backtest)

Written 2026-11-12 (owner research request 2026-11-12). **RESEARCH ONLY: no live card, pick, model, filter, ranking, alert, push, page or ledger file changed.** Every number below is a backtest on 2023-24, 2024-25 and 2025-26 (nothing fitted or chosen on them), at ESPN's stored opening line and price, except the live-ledger comparison, which is labelled. Main figures are at the pretip prediction point; morning is in the tables.

## Plain-English answers

1. **Would threes picks chosen by today's OG rules have won?** Modestly: top 5 a day 963-862 (52.8%, +3.2% ROI, 3.7 a day); top 3 652-556 (54.0%, +4.0%). Almost all are unders (1758 of 1825); 0.5 lines and bench-level shooters lost, 3.5+ lines and starter-level shooters carried it. The model is worse than the no-vig opening line on Brier over every priced row (0.24542 vs 0.24278), so the edge is narrow.
2. **Does adding threes help the card?** Top 5: assists alone 1034-935 (52.5%, +3.9%, 3.9/day) vs assists + threes 1187-969 (55.1%, +6.4%, 4.3/day): higher win rate and more picks a day. Top 3: 53.6% (+4.7%) vs 54.1% (+3.1%): a higher win rate but a lower ROI (threes take the slots at shorter prices). On the full card with Rules v2, adding threes restores most of what Rules v2 removed (table below).
3. **Rules v2 on the old seasons:** (i) unders on assist lines 5.5+ — **did not hold up**: the removed picks went 666-482 (+14.6% ROI), better than the kept ones (-1.3%), though the edge shrank every season (by season below). (ii) assist unders with 2+ key teammates out — **held up on old seasons**: removed 439-457 (-0.8%) vs kept +6.0%. (iii) rebounds — **did not hold up**: rebounds under Ranking v2 went 3061-2900 (51.3%, +5.5%) on old seasons, but live F rebounds went 168-214 (44.0%, -4.5%).
4. **How far to trust a backtest:** live F rebounds since Oct 29 trail this pipeline's F-style rebounds candidates by -3.6 pp of win rate and -5.8 pp of ROI (and the M15-rule backtest's +10.8% ROI by far more); live assists ran better than the backtest (+5.1 pp, +10.1 pp). A rebounds-sized gap would turn threes' +3.2% into about -2.6%: at best a small edge, at worst a loss. Samples are small (live: a few hundred picks).
5. **Live lines:** yes. SharpAPI's free plan already delivers DK and FD made-threes props and the poller stores them (about 11.9 players a game per book, first seen a median 14.0 h before tip, the same sweep as rebounds and assists); The Odds API's `player_threes` also quotes DK and FD (2 credits used for this check). Live box scores store fg3m for grading. Threes could go live without a new source.
6. **The full card three ways (top 5 a day, pretip):** Ranking v2 on assists + rebounds 1213-988 (55.1%, +7.5% ROI, 4.4 a day); + Rules v2 (today's OG) 923-892 (50.8%, +3.0% ROI, 3.6 a day); + Rules v2 + threes 1144-954 (54.5%, +6.1% ROI, 4.2 a day). On the old seasons Rules v2 cost win rate and units, and threes put most of it back.

## Threes (top 3 / top 5 a day, by season)

| | Picks | W-L-P | Win % (95%) | Units (1u) | ROI | Avg odds | Picks/day |
|---|---|---|---|---|---|---|---|
| pretip top 3, all | 1208 | 652-556 | 54.0% (51.1-56.8) | +48.0u | +4.0% | -106 | 2.45 |
| pretip top 3, 2023-24 | 559 | 309-250 | 55.3% (51.1-59.4) | +29.3u | +5.2% | -109 | 2.82 |
| pretip top 3, 2024-25 | 434 | 228-206 | 52.5% (47.8-57.2) | +11.7u | +2.7% | -104 | 2.06 |
| pretip top 3, 2025-26 | 215 | 115-100 | 53.5% (46.8-60.0) | +7.0u | +3.2% | -106 | 2.53 |
| pretip top 5, all | 1825 | 963-862 | 52.8% (50.5-55.0) | +58.2u | +3.2% | -103 | 3.69 |
| pretip top 5, 2023-24 | 864 | 459-405 | 53.1% (49.8-56.4) | +23.4u | +2.7% | -105 | 4.36 |
| pretip top 5, 2024-25 | 637 | 323-314 | 50.7% (46.8-54.6) | +1.7u | +0.3% | -101 | 3.02 |
| pretip top 5, 2025-26 | 324 | 181-143 | 55.9% (50.4-61.2) | +33.1u | +10.2% | -102 | 3.81 |
| pretip every Ranking v2 pick | 3921 | 1989-1932 | 50.7% (49.2-52.3) | +134.4u | +3.4% | +105 | 7.94 |
| pretip every F-style candidate (before Ranking v2) | 7322 | 3367-3955 | 46.0% (44.9-47.1) | +52.4u | +0.7% | +122 | – |
| morning top 3, all | 1214 | 653-561 | 53.8% (51.0-56.6) | +42.2u | +3.5% | -107 | 2.46 |
| morning top 3, 2023-24 | 559 | 307-252 | 54.9% (50.8-59.0) | +24.2u | +4.3% | -109 | 2.82 |
| morning top 3, 2024-25 | 440 | 229-211 | 52.0% (47.4-56.7) | +6.8u | +1.5% | -105 | 2.09 |
| morning top 3, 2025-26 | 215 | 117-98 | 54.4% (47.7-60.9) | +11.3u | +5.2% | -106 | 2.53 |
| morning top 5, all | 1838 | 974-864 | 53.0% (50.7-55.3) | +63.9u | +3.5% | -103 | 3.72 |
| morning top 5, 2023-24 | 868 | 464-404 | 53.5% (50.1-56.8) | +27.5u | +3.2% | -105 | 4.38 |
| morning top 5, 2024-25 | 646 | 327-319 | 50.6% (46.8-54.5) | +0.0u | +0.0% | -101 | 3.06 |
| morning top 5, 2025-26 | 324 | 183-141 | 56.5% (51.0-61.8) | +36.4u | +11.2% | -102 | 3.81 |
| morning every Ranking v2 pick | 4067 | 2052-2015 | 50.4% (48.9-52.0) | +112.1u | +2.8% | +105 | 8.23 |
| morning every F-style candidate (before Ranking v2) | 7470 | 3438-4032 | 46.0% (44.9-47.2) | +41.8u | +0.6% | +121 | – |

### Threes top 5 (pretip) breakdowns

| | Picks | W-L-P | Win % (95%) | Units (1u) | ROI | Avg odds | Picks/day |
|---|---|---|---|---|---|---|---|
| side: over | 67 | 32-35 | 47.8% (36.2-59.5) | -2.5u | -3.7% | +104 | – |
| side: under | 1758 | 931-827 | 53.0% (50.6-55.3) | +60.8u | +3.5% | -103 | – |
| line: 0.5 | 280 | 131-149 | 46.8% (41.0-52.6) | -19.8u | -7.1% | +101 | – |
| line: 1.5 | 782 | 410-372 | 52.4% (48.9-55.9) | +19.4u | +2.5% | -102 | – |
| line: 2.5 | 497 | 265-232 | 53.3% (48.9-57.7) | +27.6u | +5.5% | -102 | – |
| line: 3.5+ | 266 | 157-109 | 59.0% (53.0-64.8) | +31.0u | +11.7% | -110 | – |
| money: minus | 985 | 554-431 | 56.2% (53.1-59.3) | +34.1u | +3.5% | -119 | – |
| money: plus | 840 | 409-431 | 48.7% (45.3-52.1) | +24.1u | +2.9% | +112 | – |
| prob: 50-55 | 245 | 114-131 | 46.5% (40.4-52.8) | +1.2u | +0.5% | +117 | – |
| prob: 55-60 | 747 | 385-362 | 51.5% (48.0-55.1) | +29.6u | +4.0% | +102 | – |
| prob: 60+ | 829 | 461-368 | 55.6% (52.2-59.0) | +24.6u | +3.0% | -116 | – |
| prob: <50 | 4 | 3-1 | 75.0% (30.1-95.4) | +2.9u | +71.2% | +128 | – |
| role: bench-level | 844 | 432-412 | 51.2% (47.8-54.5) | -5.4u | -0.6% | -104 | – |
| role: starter-level | 981 | 531-450 | 54.1% (51.0-57.2) | +63.7u | +6.5% | -102 | – |

Starter-level = projected minutes >= 28 (nbap15's minutes model).

## Assists and rebounds under Ranking v2 (no Rules v2)

| | Picks | W-L-P | Win % (95%) | Units (1u) | ROI | Avg odds | Picks/day |
|---|---|---|---|---|---|---|---|
| assists pretip top 3 | 1280 | 686-594 | 53.6% (50.8-56.3) | +59.9u | +4.7% | -104 | 2.55 |
| assists pretip top 5 | 1969 | 1034-935 | 52.5% (50.3-54.7) | +76.0u | +3.9% | -101 | 3.92 |
| assists morning top 3 | 1285 | 686-599 | 53.4% (50.6-56.1) | +52.6u | +4.1% | -104 | 2.56 |
| assists morning top 5 | 1976 | 1048-928 | 53.0% (50.8-55.2) | +93.5u | +4.7% | -102 | 3.94 |
| rebounds pretip top 3 | 1271 | 709-562 | 55.8% (53.0-58.5) | +125.1u | +9.8% | -102 | 2.52 |
| rebounds pretip top 5 | 1987 | 1062-925 | 53.4% (51.2-55.6) | +123.3u | +6.2% | -100 | 3.94 |
| rebounds morning top 3 | 1277 | 723-554 | 56.6% (53.9-59.3) | +143.5u | +11.2% | -103 | 2.53 |
| rebounds morning top 5 | 1990 | 1070-920 | 53.8% (51.6-56.0) | +130.9u | +6.6% | -101 | 3.95 |

## The combined assists + threes card (Ranking v2, no Rules v2)

| | Picks | W-L-P | Win % (95%) | Units (1u) | ROI | Avg odds | Picks/day |
|---|---|---|---|---|---|---|---|
| pretip top 3: assists alone | 1280 | 686-594 | 53.6% (50.8-56.3) | +59.9u | +4.7% | -104 | 2.55 |
| pretip top 3: assists + threes (threes 57% of picks) | 1367 | 739-628 | 54.1% (51.4-56.7) | +43.0u | +3.1% | -108 | 2.72 |
| pretip top 3: same player-game in both markets | 68 pairs: both won 19, both lost 14, split 35 | | | | | | |
| pretip top 5: assists alone | 1969 | 1034-935 | 52.5% (50.3-54.7) | +76.0u | +3.9% | -101 | 3.92 |
| pretip top 5: assists + threes (threes 55% of picks) | 2156 | 1187-969 | 55.1% (52.9-57.1) | +138.9u | +6.4% | -106 | 4.29 |
| pretip top 5: same player-game in both markets | 145 pairs: both won 40, both lost 36, split 69 | | | | | | |
| morning top 3: assists alone | 1285 | 686-599 | 53.4% (50.6-56.1) | +52.6u | +4.1% | -104 | 2.56 |
| morning top 3: assists + threes (threes 56% of picks) | 1377 | 729-648 | 52.9% (50.3-55.6) | +11.8u | +0.9% | -108 | 2.74 |
| morning top 3: same player-game in both markets | 73 pairs: both won 21, both lost 15, split 37 | | | | | | |
| morning top 5: assists alone | 1976 | 1048-928 | 53.0% (50.8-55.2) | +93.5u | +4.7% | -102 | 3.94 |
| morning top 5: assists + threes (threes 55% of picks) | 2165 | 1180-985 | 54.5% (52.4-56.6) | +111.4u | +5.1% | -106 | 4.31 |
| morning top 5: same player-game in both markets | 153 pairs: both won 47, both lost 39, split 67 | | | | | | |

## Rules v2 on the old seasons (assists and rebounds)

| | Picks | W-L-P | Win % (95%) | Units (1u) | ROI | Avg odds | Picks/day |
|---|---|---|---|---|---|---|---|
| pretip (i) assist unders 5.5+: removed | 1148 | 666-482 | 58.0% (55.1-60.8) | +167.4u | +14.6% | -101 | – |
| pretip (i) every other assists pick: kept | 4355 | 2071-2284 | 47.5% (46.1-49.0) | -57.8u | -1.3% | +109 | – |
| pretip (i) removed, 2023-24 | 749 | 463-286 | 61.8% (58.3-65.2) | +161.8u | +21.6% | -102 | – |
| pretip (i) removed, 2024-25 | 253 | 131-122 | 51.8% (45.6-57.9) | +8.7u | +3.5% | +101 | – |
| pretip (i) removed, 2025-26 | 146 | 72-74 | 49.3% (41.3-57.3) | -3.1u | -2.1% | -101 | – |
| pretip (ii) assists unders, 2+ key teammates out: removed | 896 | 439-457 | 49.0% (45.7-52.3) | -7.5u | -0.8% | +103 | – |
| pretip (ii) assists unders, fewer: kept | 2735 | 1421-1314 | 52.0% (50.1-53.8) | +163.5u | +6.0% | +105 | – |
| pretip (ii) rebounds unders, 2+ key teammates out: removed | 1232 | 681-551 | 55.3% (52.5-58.0) | +148.2u | +12.0% | +103 | – |
| pretip (ii) rebounds unders, fewer: kept | 4140 | 2105-2035 | 50.8% (49.3-52.4) | +175.7u | +4.2% | +106 | – |
| pretip (iii) rebounds under Ranking v2 | 5961 | 3061-2900 | 51.3% (50.1-52.6) | +325.1u | +5.5% | +106 | – |
| pretip (iii) rebounds, 2023-24 | 3616 | 1884-1732 | 52.1% (50.5-53.7) | +249.6u | +6.9% | +106 | – |
| pretip (iii) rebounds, 2024-25 | 1556 | 781-775 | 50.2% (47.7-52.7) | +49.0u | +3.1% | +107 | – |
| pretip (iii) rebounds, 2025-26 | 789 | 396-393 | 50.2% (46.7-53.7) | +26.5u | +3.4% | +107 | – |
| morning (i) assist unders 5.5+: removed | 1201 | 687-514 | 57.2% (54.4-60.0) | +155.3u | +12.9% | -101 | – |
| morning (i) every other assists pick: kept | 4386 | 2089-2297 | 47.6% (46.2-49.1) | -57.6u | -1.3% | +108 | – |
| morning (i) removed, 2023-24 | 779 | 480-299 | 61.6% (58.1-65.0) | +164.7u | +21.1% | -102 | – |
| morning (i) removed, 2024-25 | 272 | 136-136 | 50.0% (44.1-55.9) | -0.5u | -0.2% | +101 | – |
| morning (i) removed, 2025-26 | 150 | 71-79 | 47.3% (39.5-55.3) | -8.9u | -5.9% | -101 | – |
| morning (ii) assists unders, 2+ key teammates out: removed | 665 | 321-344 | 48.3% (44.5-52.1) | -14.7u | -2.2% | +103 | – |
| morning (ii) assists unders, fewer: kept | 3164 | 1620-1544 | 51.2% (49.5-52.9) | +134.3u | +4.2% | +105 | – |
| morning (ii) rebounds unders, 2+ key teammates out: removed | 895 | 495-400 | 55.3% (52.0-58.5) | +111.1u | +12.4% | +104 | – |
| morning (ii) rebounds unders, fewer: kept | 4698 | 2403-2295 | 51.1% (49.7-52.6) | +214.7u | +4.6% | +105 | – |
| morning (iii) rebounds under Ranking v2 | 6180 | 3183-2997 | 51.5% (50.3-52.8) | +345.7u | +5.6% | +106 | – |
| morning (iii) rebounds, 2023-24 | 3737 | 1957-1780 | 52.4% (50.8-54.0) | +271.1u | +7.3% | +106 | – |
| morning (iii) rebounds, 2024-25 | 1613 | 809-804 | 50.1% (47.7-52.6) | +46.5u | +2.9% | +106 | – |
| morning (iii) rebounds, 2025-26 | 830 | 417-413 | 50.2% (46.9-53.6) | +28.1u | +3.4% | +107 | – |

(ii) reads each team's key players (>= 24 min over their last 10 games before the prediction point) and the injury report known at the point, with the live rule's own code; a report was known for 99.7% of assist unders (no season skipped). Live, (ii) applies to assists only; the rebounds row is for information. Verdicts (pretip): (i) did not hold up; (ii) held up on old seasons; (iii) did not hold up.

## The full card three ways (top 3 / top 5 a day)

| | Picks | W-L-P | Win % (95%) | Units (1u) | ROI | Avg odds | Picks/day |
|---|---|---|---|---|---|---|---|
| pretip top 3: Ranking v2 (assists + rebounds) | 1380 | 774-606 | 56.1% (53.5-58.7) | +112.9u | +8.2% | -106 | 2.75 |
| pretip top 3: + Rules v2 (today's OG) | 1203 | 633-570 | 52.6% (49.8-55.4) | +65.0u | +5.4% | +101 | 2.40 |
| pretip top 3: + Rules v2 + threes | 1338 | 730-608 | 54.6% (51.9-57.2) | +65.4u | +4.9% | -107 | 2.67 |
| pretip top 5: Ranking v2 (assists + rebounds) | 2201 | 1213-988 | 55.1% (53.0-57.2) | +165.6u | +7.5% | -104 | 4.38 |
| pretip top 5: + Rules v2 (today's OG) | 1815 | 923-892 | 50.8% (48.5-53.1) | +54.6u | +3.0% | +104 | 3.62 |
| pretip top 5: + Rules v2 + threes | 2098 | 1144-954 | 54.5% (52.4-56.6) | +128.9u | +6.1% | -104 | 4.18 |
| morning top 3: Ranking v2 (assists + rebounds) | 1387 | 769-618 | 55.4% (52.8-58.0) | +93.5u | +6.7% | -106 | 2.76 |
| morning top 3: + Rules v2 (today's OG) | 1216 | 644-572 | 53.0% (50.1-55.8) | +68.1u | +5.6% | +100 | 2.42 |
| morning top 3: + Rules v2 + threes | 1346 | 719-627 | 53.4% (50.7-56.1) | +33.3u | +2.5% | -107 | 2.68 |
| morning top 5: Ranking v2 (assists + rebounds) | 2212 | 1220-992 | 55.1% (53.1-57.2) | +163.5u | +7.4% | -105 | 4.41 |
| morning top 5: + Rules v2 (today's OG) | 1842 | 951-891 | 51.6% (49.4-53.9) | +78.6u | +4.3% | +103 | 3.67 |
| morning top 5: + Rules v2 + threes | 2110 | 1141-969 | 54.1% (51.9-56.2) | +107.2u | +5.1% | -105 | 4.20 |

## Pool checks (every priced row, pretip; no true pre-tip close for most rows)

| Market | Rows | Brier model | Brier no-vig open | EV at open no-vig, top 5 |
|---|---|---|---|---|
| threes | 28592 | 0.24542 | 0.24278 | -6.2% |
| assists | 32170 | 0.24652 | 0.24557 | -6.3% |
| rebounds | 35133 | 0.24717 | 0.2472 | -6.2% |

EV at the opening no-vig is roughly minus the vig by construction (the pick's price is the opening price); it is reported because no pre-tip close exists for most historical rows (ESPN keeps the opening line and an in-game quote).

### Calibration of P(over), pretip (every priced row)

| Market | Bin | n | Predicted | Actual |
|---|---|---|---|---|
| threes | (0.0, 0.2] | 18 | 0.145 | 0.111 |
| threes | (0.2, 0.3] | 109 | 0.266 | 0.404 |
| threes | (0.3, 0.4] | 4233 | 0.375 | 0.386 |
| threes | (0.4, 0.5] | 17728 | 0.45 | 0.465 |
| threes | (0.5, 0.6] | 6357 | 0.53 | 0.585 |
| threes | (0.6, 0.7] | 144 | 0.618 | 0.632 |
| threes | (0.7, 0.8] | 2 | 0.717 | 0.5 |
| threes | (0.8, 1.0] | 1 | 0.814 | 1.0 |
| assists | (0.0, 0.2] | 72 | 0.067 | 0.028 |
| assists | (0.2, 0.3] | 84 | 0.265 | 0.262 |
| assists | (0.3, 0.4] | 990 | 0.372 | 0.413 |
| assists | (0.4, 0.5] | 18076 | 0.464 | 0.454 |
| assists | (0.5, 0.6] | 12602 | 0.531 | 0.541 |
| assists | (0.6, 0.7] | 331 | 0.623 | 0.656 |
| assists | (0.7, 0.8] | 13 | 0.73 | 0.769 |
| assists | (0.8, 1.0] | 2 | 0.81 | 0.5 |
| rebounds | (0.0, 0.2] | 78 | 0.077 | 0.0 |
| rebounds | (0.2, 0.3] | 16 | 0.256 | 0.188 |
| rebounds | (0.3, 0.4] | 380 | 0.375 | 0.405 |
| rebounds | (0.4, 0.5] | 29239 | 0.468 | 0.465 |
| rebounds | (0.5, 0.6] | 5398 | 0.515 | 0.552 |
| rebounds | (0.6, 0.7] | 19 | 0.637 | 0.842 |
| rebounds | (0.7, 0.8] | 3 | 0.718 | 1.0 |

### Walk-forward F-style Platt fit on the picked side (top 5, pretip)

Fitted per season on the earlier seasons' candidates only (2022-23 first). The fit is increasing, so the ranking is unchanged (checked).

| Market | Season | Fit on (n) | Slope | Raw mean p | Calibrated expectation | Actual win | Ranking unchanged |
|---|---|---|---|---|---|---|---|
| threes | 2023 | 4157 | 0.6884 | 60.3% | 49.1% | 53.1% | True |
| threes | 2024 | 8188 | 0.8479 | 59.0% | 49.2% | 50.7% | True |
| threes | 2025 | 10316 | 0.8402 | 60.2% | 50.3% | 55.9% | True |
| assists | 2023 | 6765 | 1.0886 | 59.8% | 51.6% | 52.5% | True |
| assists | 2024 | 13232 | 1.0791 | 57.0% | 49.7% | 51.0% | True |
| assists | 2025 | 16369 | 1.0442 | 57.2% | 50.1% | 55.7% | True |
| rebounds | 2023 | 6483 | 1.3338 | 57.6% | 53.2% | 53.7% | True |
| rebounds | 2024 | 11992 | 1.4399 | 56.0% | 52.1% | 51.7% | True |
| rebounds | 2025 | 15108 | 1.5044 | 55.7% | 51.9% | 56.2% | True |

## Backtest vs live (the discount)

Live = the ledger's F picks since Oct 29, one per player-game-market (graded only, 1u). Backtest = this pipeline's F-style candidates (pretip, before Ranking v2).

| Market | Live W-L | Live win % | Live ROI | Backtest win % | Backtest ROI | Gap win | Gap ROI |
|---|---|---|---|---|---|---|---|
| rebounds | 168-214 | 44.0% | -4.5% | 47.5% | +1.3% | -3.6 pp | -5.8 pp |
| assists | 171-165 | 50.9% | +10.5% | 45.8% | +0.3% | +5.1 pp | +10.1 pp |

Ranking v2 style: the walk-forward tool's Ranking v2 top 5 on the live ledger (Oct 31 - Nov 11, in-sample for the rule design) went 23-21-0 (52.3%, +4.1% ROI at 0.5u); this pipeline's today's-OG card (Ranking v2 + Rules v2, top 5, pretip) went 923-892 (50.8%).

## Checks

- **The rule replicates live OG Ranking v2:** the same top 5 on 10 of 11 live card days (Oct 31 - Nov 11, the ledger pool); the one difference (2026-11-04) is a full tie — the same bet at DraftKings and FanDuel, same price, probability and creation second — where the live code keeps one book and the replica the other.
- **Reconciles with the stored backtests:** under the M15 rule (10 pp, no Ranking v2) this pipeline gives rebounds pretip 1977 bets 1005-972 (+10.8% ROI; stored 1976, 1005-971) and assists pretip 3021 bets 1438-1583 (+5.1%; stored 3020, 1437-1583), matching props_median_backtest.json within one bet (props_final_test.json predates the M16 line audit; props_final_test_m16_audit.json differs by under 1% as the stored lines grew).
- **No leakage:** the model is nbap15's final-test fit (2015-16 to 2022-23); the opening line was posted before both prediction points; the Platt checks fit only earlier seasons; the teammates rule reads key players from games before the point and injury reports known at it (tests/test_threes_bt.py).
- F's consensus check (other books not siding against) cannot be replayed: the history keeps one book per line.

## Options for the owner (not decided here)

1. **Threes as Action-only picks:** evidence for — threes top 5 52.8% / +3.2% on the old seasons, lines already flowing, no change to OG's record; against — a rebounds-sized live discount would erase the edge, and threes picks are nearly all unders.
2. **Threes on the official OG card after an earn-in bar** (e.g. live Action threes at .500+ and positive units over a fixed number of graded picks): for — it raises the full card's win rate and picks per day in the backtest; against — the official record must stay above .500, and rebounds show how far live can fall below a backtest.
3. **Threes not worth it:** for — Brier worse than the no-vig line, thin ROI, 0.5 lines and bench shooters lose; against — it is the best-performing third market under today's rules and costs no new data.

- **Rule (i) unders 5.5+ assists:** did not hold up on old seasons — keep or revisit at the Filters review (its removed picks were profitable, but less each season).
- **Rule (ii) teammates out:** held up on old seasons — keep or revisit at the Filters review.
- **Rule (iii) no rebounds:** did not hold up on old seasons, but live rebounds lost — the case for keeping it rests on live data, the case for revisiting on the backtest.

Nothing here changes a live rule; the owner decides at the Filters review.

