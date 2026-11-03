# M19 report: one official record, a game-first page, a reason for every result

Written 2026-11-03. Presentation and review fields only.

**Unchanged:** models, signals, thresholds, grading rules, card selection rule and data sources. Signals stay frozen for their 200-pick tests.

**Ledger:** still append-only, with two new write-once files (`review_pick.csv`, `review_result.csv`). The git-history test enforces append-only on every ledger file, and no stored pick or result field changed.

Live page: https://piratekinginc.github.io/test-m11/

## The official record (card picks, graded only)

A record counts graded picks only, like a team's record (owner correction 2026-11-03). It shows W-L-P (always with units), units at the card stake, ROI, average odds (mean decimal, shown American), CLV vs the consensus close, and the number graded.

| Official | W-L-P | Units | ROI | Avg odds | CLV | Graded |
|---|---|---|---|---|---|---|
| All sports, all time | 6-5-0 | +1.07u | +19.5% | +119 | +2.92 pp | 11 |
| NBA, all time | 6-4-0 | +1.57u | +31.4% | +122 | +2.94 pp | 10 |
| NFL, all time | 0-1-0 | −0.50u | −100.0% | −110 | +2.72 pp | 1 |

- **Pending:** today's 5 NBA card picks appear only under Today ("Tonight: 5 card picks pending"), never in a record.
- **Voids:** none on the card so far. They would be listed only in the day view.
- **Caveat:** 11 graded picks is far too few to judge anything.

## Worked example: Sun Nov 1 (NBA card)

Day record: **3-2-0 · +0.69u · ROI +27.6% · avg odds +118 · CLV +2.5 pp · 5 graded**. It matches `site/csv/nba-day-2026-11-01.csv` and the ledger. All picks are LEAN 0.5u.

| Pick | Team vs Opp | Odds, book | Result | Actual | Units | CLV | Why |
|---|---|---|---|---|---|---|---|
| Derrick White under 4.5 assists | BOS vs ORL | +106 DraftKings | WIN | 2 | +0.53 | −1.46 pp | Market disagreed |
| Anthony Black under 3.5 assists | ORL vs BOS | +116 FanDuel | WIN | 3 | +0.58 | +1.44 pp | Variance |
| Jarace Walker over 2.5 assists | IND vs BKN | +116 DraftKings | WIN | 3 | +0.58 | +4.44 pp | Minutes miss (proj 28.4, played 20) |
| Brandon Williams under 2.5 rebounds | GSW vs LAC | +136 FanDuel | LOSS | 3 | −0.50 | +8.11 pp | Minutes miss (proj 20.0, played 25) |
| Isaiah Jackson under 6.5 rebounds | LAC vs GSW | +118 FanDuel | LOSS | 8 | −0.50 | +0.02 pp | Variance |

The Nov 1 NFL card (IND @ JAX under 46.5, a C-NFL pick) lost: actual total 49, −0.50u. Its tag is "not enough data" because no NFL injury report for that week was in the database at pick time.

## Reason tags so far (official, graded)

- **Wins (6):** 2 Variance, 2 Minutes miss, 1 News after pick, 1 Market disagreed.
- **Losses (5):** 2 Variance, 1 Minutes miss, 2 not enough data (Dennis Schroder: no NBA injury report known at pick time for PHI @ CHA; the NFL total above).
- **Example of News after pick:** Kevin Durant over 3.5 assists (Oct 31) won after Fred VanVleet went Available → Out between the pick and tip.

Backfill: all 449 reviewable picks have pick-time review rows. The 15 official NBA props without the alert's own minutes got nbap15 re-run as of the pick time. Research picks without minutes stay "not enough data" for the minutes rule, as MODEL.md allows.

## Research signals (Research tab: every graded pick, flat 1 unit, not official)

| Signal | W-L-P | Units | ROI | Avg odds | CLV | Graded | 200-pick gate |
|---|---|---|---|---|---|---|---|
| C-NBA | 4-7-0 | −2.78u | −25.2% | −102 | +1.74 pp | 11 | no read before 60 |
| C-NFL | 0-1-0 | −1.00u | −100.0% | −110 | +2.72 pp | 1 | no read before 60 |
| F | 80-101-0 | −6.60u | −3.6% | +118 | +1.66 pp | 181 | early read: positive, range excludes 0 (179 with CLV) |
| G1 | 4-7-0 | −3.36u | −30.5% | −110 | +0.23 pp | 11 | no read before 60 |
| G2 | 74-25-0 | −12.59u | −12.7% | −281 | +2.04 pp | 99 | early read: positive, range excludes 0 |
| sharp-nba | 0-1-0 | −1.00u | −100.0% | −198 | +0.00 pp | 1 | no read before 60 |

G2 is the case M19 was built to expose: 74-25 but −12.59 units at an average of −281. A W-L that looks good loses money when the prices are that short.

The brief quoted average odds of −1208. That figure is a mean of American odds, which very short favourites inflate. MODEL.md fixes the mean of decimal odds instead.

## What was built

- **One function over the ledger (`betdata/official.py`):** official and research records (graded only), periods, by day, tag counts and the daily CSV.
- **Page (each sport tab):**
  - **Official record:** all time, last 7 days and yesterday, each number linked to a CSV of its graded picks.
  - **Today:** game dropdowns (time ET, teams, live score or status, ★ card count). Card picks come first, then "Other model picks (research, not official)" grouped by team. Rows read "Player (TEAM) vs OPP", and this is the only place pending picks are counted.
  - **Results by day:** newest first. Opening a day shows its picks with result, actual stat and reason tag, plus its voids, tag counts and CSV. A date field jumps to any day.
  - **Research tab:** signal records and gates, the model-vs-market and engine sections, props, and the other model versions.
- **Removed:** the Value list, card and labelled-pick bankroll boxes, the separate tracker list, Past picks and duplicated records.
- **Review fields (`betdata/review.py`):** write-once pick-time fields (team, projections, line, assumed statuses) and result fields (actual stat and minutes, status changes, close, CLV, tag). Tag rules are fixed in MODEL.md.
- **Weekly report:** the official card record, reason tags and one line per research signal with its gate.

## Verification

1. **Regression:** full suite 664 passed (sqlite and postgres CI green on every PR). The ledger append-only tests pass, including the git history of all eight ledger files. No stored pick field changed.
2. **Reconciliation:** page totals equal an independent recount of the raw CSVs, written in plain `csv` code with no betdata import (`tests/test_official.py`, `tests/runner/test_page_m19.py`). This holds on fixtures and on the real ledger, for official all-time, three past days (Nov 2, Nov 1, Oct 31) and each signal.
3. **Playwright at 390 px, on the live page:**
   - order: official, Today, Results, data sources;
   - every pick row has "(TEAM) vs OPP" (141 NBA rows, 1 NFL row);
   - the deepest game row is 1.75 screen-heights down, and its picks open in 2 taps (tab, game);
   - a past day opens with all picks, stats and tags;
   - the date jump works;
   - nothing overflows (scrollWidth 390) with everything open;
   - no page errors.
4. **Fixture day with a win, a loss, a void and a pending pick:** the record reads "1-1-0 · +0.10u · … · 2 graded" in every period and on the day. The void is listed only in the day view, and the pending pick appears only under Today ("Tonight: 1 card pick pending"). Screenshot: `reports/m19/fixture_win_loss_void_pending.png`.
5. **Live proof, two consecutive automatic hourly runs on the final code (#288):**
   - 01:00 UTC, run 355 (`github-actions[bot]`, clock), success. Page generated 01:01:47; the records were graded only and Today read "Tonight: 5 card picks pending".
   - 02:00 UTC: see below.

Screenshots in `reports/m19/`:
- `live_main_view.png`
- `live_official_record_nba.png`
- `live_opened_game.png`
- `live_past_day.png`
- `live_research.png`
- the `fixture_*` set

## Issues and PRs

- **Issues:** #273 official record, #274 main view, #275 games with team names, #276 results by day, #277 Research tab, #278 check the work, #279 review fields and tags, #280 backfill, #281 weekly report, #282 docs, verification and report.
- **PRs:** #283 plan (MODEL.md), #284 official record module, #285 review fields and tags, #286 page, #287 weekly report, #288 graded-only correction, plus this report.

## What still can't be answered from the page

- **Why an NFL pick won or lost**, usually. NFL injury reports are weekly and inactives aren't stored, so status changes after an NFL pick are rarely visible; "not enough data" is common there.
- **Minutes-based reasons for research picks.** Only official picks get the as-of projection rebuild; research picks from before M19 have no stored minutes. New picks store them at pick time from now on.
- **Live player stats during a game.** There's no free, terms-compliant source, so player results appear at the final. Live scores also need the optional balldontlie key.
- **Statistical significance.** 11 graded official picks prove nothing either way; the 200-pick gates are still far off for every signal except F (179/200 with CLV).
