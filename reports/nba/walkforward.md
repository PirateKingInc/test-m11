# Walk-forward: OG card rules (2026-11-12)

Report only (owner 2026-11-12): nothing here changes a card. Each card day is evaluated at its stored selection time with picks created before it; calibration fits use only picks graded before it. One pick per player per game. Units at 0.5u; graded picks only.

Card days: 10 (2026-10-31 to 2026-11-11; Nov 5 excluded). Pool, all days: 621 raw candidate picks created before selection → 140 after Rules v2 and Ranking v2's price / median rules → 120 after one pick per player per game.

**In-sample vs out-of-sample, plainly:** Ranking v2's rules (price range, median side, ranking by probability) were chosen by the owner after looking at Oct 31 - Nov 10, so rules (ii)-(iv) on those days are **in-sample** for the rule design even though no pick or grade from the future is used. Only days from Nov 12 on are out-of-sample for the rules. The calibration fits in (iv) and (v) are walk-forward, i.e. out-of-sample for their parameters on every day.

## All card days (rules (ii)-(iv) in-sample for the rule design)

| Rule | Record | Win % | Units (0.5u) | ROI | Avg odds | Picks/day | Worst 3 days |
|---|---|---|---|---|---|---|---|
| (i) Actual OG card | 20-25-0 | 44.4% | +0.50 | +2.2% | +130 | 5.0 | 4-11, -2.96u (2026-11-08 to 2026-11-10) |
| (ii) Ranking v2 top 5 | 21-19-0 | 52.5% | +0.80 | +4.0% | -100 | 4.8 | 5-7, -0.94u (2026-11-07 to 2026-11-09) |
| (iii) Ranking v2 top 3 | 16-9-0 | 64.0% | +3.36 | +26.8% | -105 | 3.0 | 4-4, -0.15u (2026-10-31 to 2026-11-02) |
| (iv) Ranking v2 on calibrated p (walk-forward fits) | 21-19-0 | 52.5% | +0.80 | +4.0% | -100 | 4.8 | 5-7, -0.94u (2026-11-07 to 2026-11-09) |
| (v) Research: per-segment calibration (walk-forward) | 21-19-0 | 52.5% | +0.80 | +4.0% | -100 | 4.8 | 5-7, -0.94u (2026-11-07 to 2026-11-09) |

- (iv) vs (ii): the same picks in the same order on 10 of 10 days (calibration is increasing, so it cannot change the order).
- (v) vs (ii): the same picks in the same order on 9 of 10 days.

## Split: parameters frozen before 2026-11-12, scored from it (out-of-sample)

| Rule | Record | Win % | Units (0.5u) | ROI | Avg odds | Picks/day | Worst 3 days |
|---|---|---|---|---|---|---|---|
| (i) Actual OG card | 0-0-0 | – | +0.00 | – | – | – | 0-0, +0.00u (–) |
| (ii) Ranking v2 top 5 | 0-0-0 | – | +0.00 | – | – | – | 0-0, +0.00u (–) |
| (iii) Ranking v2 top 3 | 0-0-0 | – | +0.00 | – | – | – | 0-0, +0.00u (–) |
| (iv) Ranking v2 on calibrated p (walk-forward fits) | 0-0-0 | – | +0.00 | – | – | – | 0-0, +0.00u (–) |
| (v) Research: per-segment calibration (walk-forward) | 0-0-0 | – | +0.00 | – | – | – | 0-0, +0.00u (–) |

Days scored: 0 — too few to read anything yet.

## Differences from the runner replay (reports/nba/og_ranking_v2.md)

This tool reads the ledger only. It cannot apply the Live check (no quotes in the ledger), so its pool is every pick created before selection — compare it with the replay's "every pick created before selection" rows (top 5 22-16, top 3 17-8, Oct 31 - Nov 10). The teammates-out rule applies only where the ledger recorded it (from Nov 10); before that the runner replay used the injury report known at selection and removed some unders this tool keeps (on the first run: Anthony Black and Desmond Bane on Nov 1 and Nov 7, Coby White and Naz Reid on Nov 9), which is the whole difference in (ii) on those days. This tool also includes Nov 11 (pending at the first run). The median is `review_pick.proj_median`, the field the live rule reads.

