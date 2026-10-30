# M18 final report: stale prices, not model edges

Written 2026-10-29/30. M18 rebuilt the project around one idea: **the de-vigged consensus is the
true price, and value is a DK/FD price that has not caught up with news.** The models stopped
betting against the market. They now only reprice on news. MODEL.md "M18" holds the rules,
fixed before any result.

Unchanged by M18:
- existing model versions and frozen fits;
- the append-only odds history;
- existing migrations (M18 needs none: its tables live in the poller database).

The only re-fit is Part D's median-anchored variant, which is research only.

**Bottom line:**
- **Built and live:**
  - the consensus price, with every input stored;
  - our own line archive with openers;
  - news → reprice → stale-price alerts for the NBA (and the NFL as a lag study);
  - opening-line prop snipes and the two seasonal paper tests;
  - grading by CLV against the consensus close, with a 200-alert gate per signal;
  - a Value section at the top of each tab.
- **No ESPN odds or prop endpoint is called any more.**
- **Nothing is proven yet:**
  - every signal is far from its 60-alert early read;
  - the replay of M16's stored NBA news gives 17 value alerts with a mean CLV of +0.5 pp (95% range −0.3 to +1.3);
  - the G1 backtest loses (ROI −12.9%);
  - the median-anchored prop variant changes nothing material.

## 1. Flagging changes (Part A, #246)

**Model picks are research only:**
- No model-vs-market spread, moneyline or prop pick is flagged any more, in either sport.
- A threshold pick is still listed (muted, "research only, not flagged") and graded in the season records.

**The agreement rule** (owner decision of 2026-10-29) changed:
- **Dropped:** the line-move vote (Moskowitz 2021: such moves overreact; live 0-6 for m13w).
- **Kept:** "Pinnacle's no-vig sides with the model".
- **Added:** a Kalshi vote. Kalshi's fresh (≤ 10 min) yes-ask beats the DK/FD no-vig.
- **Recorded on grades:** `agree` = pinnacle / kalshi / pinnacle+kalshi. The hourly regrade rewrote the old "move" codes.

## 2. Consensus sources (Part B, #249)

| Input | Where | Status |
|---|---|---|
| Pinnacle (The Odds API, `bookmakers=pinnacle`) | poller boards (NBA 17:30/18:45 ET, NFL T−75) | used; wins whenever fresh (≤ 10 min) |
| DK, FD (SharpAPI free) | hourly job, poller sweeps, lag polls | used; the usual inputs |
| Other books on The Odds API boards | main `odds_snapshots` | used when a board is fresh |
| Kalshi | poller (winners) | a moneyline vote |
| The Odds API player props | measured live: free plan, 1 credit per market per event, 5 books | **not polled**: one NBA slate is ~32 credits a board |

**How the consensus is built:**
- **Game markets:** each book is put on a fair scale (L*, T*, p_home) and the median is taken.
- **Props:** the median no-vig at one line.
- **Labels:** "two-book" when only DK/FD quote.
- **Storage:** every consensus is stored with its inputs (`consensus`, 217 rows after the first two sweeps).
- **Freshness over change-only storage:** `poll_marks` and removal rows (price NULL when a book pulls a line).

**Honest limit:** outside Pinnacle board times the consensus is almost always two-book. In the first live sweeps, all 217 were medians of DK/FD (plus Kalshi on moneylines). So most alerts need the higher two-book bar (4.5 / 9 pp) or a confirming move.

## 3. Alerts per signal, with CLV (Part H, #252; page #253)

| Signal | What | Live alerts so far | Graded vs consensus close | CLV | Gate |
|---|---|---|---|---|---|
| C-NBA | stale DK/FD after NBA news | 1 (CHA @ WAS +1.5 −108 FD; 14 qualifying NBA changes since deploy) | 0 (tips Oct 30 23:00 UTC) | – | no read before 60 |
| C-NFL | same, NFL, "[lag study]" | 0 (no NFL window since deploy) | 0 | – | 4-week lag study running |
| F | NBA opening-line rebound/assist snipes | 13 | 6 | **+0.66 pp** (95%: −2.60 to +3.92) | no read before 60 |
| G1 | early-season NBA unders | 1 (SAC @ NOP u234.5 −108 DK; opened 236.5) | 1 | −0.65 pp | paper |
| G2 | NFL alternate rungs | 0 (next NFL board is Sunday) | 0 | – | paper |

**The replay** (verification, `reports/m18/replay_value.json`, #258) ran all 95 qualifying NBA status changes M16 stored (Oct 22-28):
- **Method:** each change went through the M18 value rule as of its detection, with the quotes of its first poll, on a copy of the poller DB.
- **Output:** 17 alerts across 10 changes, against M16 Signal 1's 6:
  - spreads 8, moneylines 6, props 3 (all three model only);
  - 16 graded against the consensus close: mean CLV **+0.51 pp**, 95% range −0.29 to +1.32.
- **Why most changes produced nothing:**
  - the books had already moved by the time M16 polled;
  - or no DK/FD quote existed before the report's stated time (M16 did not sweep), which the stale rule requires.

## 4. Replay test and median test (verification)

- **Replay test:** `tests/alerts/test_replay.py` checks that a stored event reproduces the expected alert and leaves the original DB untouched. The live run is above.
- **Median test:** `tests/props/test_median.py`:
  - quantiles are the distribution's;
  - the median round trip holds;
  - the median regression finds the conditional median on skewed data where least squares would not;
  - the page shows "Median x (mean y)".

**Median backtest** (Part D, #250, `reports/nba/props_median_backtest.json`): a repeat look at the test seasons 2023-26 at opening lines, threshold 10 pp; nothing chosen from it.

| Market (pretip) | Mean-based nbap15 | Median-anchored |
|---|---|---|
| Rebounds | 1005-971 (50.9%), ROI +10.8%, MAE 2.024, Brier 0.2472 | 975-930 (51.2%), ROI +11.6%, MAE 2.019, Brier 0.2472 |
| Assists | 1437-1583 (47.6%), ROI +5.1%, MAE 1.542, Brier 0.2465 | 1444-1601 (47.4%), ROI +4.7%, MAE 1.532, Brier 0.2465 |

- **Brier:** the no-vig line scores 0.2472 (rebounds) and 0.2456 (assists); the model only ties it.
- **ROI:** the positive ROIs come from plus-money sides at extreme edges, with sub-50% win rates.
- **Verdict:** anchoring on the median does not change the picture.

## 5. Archive and ESPN removal (Part E, #247, #248)

**Archive:**
- **Openers:** `openers` (first price per key, never overwritten) had 821 rows after two sweeps.
- **Sweeps:** 06:00, hourly 07-23 and 23:30 ET; every game in 36 h, 14 games a sweep today.
- **Quotes:** change-only, plus `poll_marks`.

**Props pipeline:** DK/FD props reach `prop_lines` from the archive every hourly run: 12,671 rows on the first run, 211 the next.

**ESPN:**
- **Odds:** no ESPN odds or prop endpoint is called. The jobs are removed, scoreboard odds are not stored, and every retired fetch raises `EspnOddsDisabled`. The no-ESPN test checks the config, the workflows and each retired path.
- **Box scores and schedules:** unchanged. The owner decision is logged in BACKLOG.
- **Score cross-check:** deferred. The Odds API `/scores` costs 2 credits; balldontlie needs a key.

## 6. NFL lag study status (Part C-NFL, #255)

- **Windows:** T−100..T−60 before each kickoff window, plus Fridays 16:00-20:00 ET.
- **Alerts:** NFL changes go to the value signal labelled "[lag study]".
- **Decision:** `decide_nfl` rules once, 28 days after the first NFL qualifying change from 2026-10-29. "Live" needs a pooled DK+FD median lag ≥ 5 min.
- **Status:** no NFL window has opened since deploy (the next is Friday's report window and Sunday's inactives). The study has 0 events.
- **Repricing:** m13w (sides) and nflp15 (props). M17 engine repricing is not wired (BACKLOG).

## 7. G1 results (#256, `reports/nba/g1_backtest.json`)

**Rule:** the under in each team's first 4 home games when the close is ≥ 0.5 below the open.

| Season | n | W-L-P | Win rate (95%) | ROI |
|---|---|---|---|---|
| All | 194 | 88-105-1 | 45.6% (38.7-52.6%) | **−12.9%** |
| 2023-24 | 68 | 30-37-1 | 44.8% | −14.3% |
| 2024-25 | 57 | 28-29-0 | 49.1% | −6.2% |
| 2025-26 | 39 | 13-26-0 | 33.3% | −36.4% |
| 2026-27 so far | 30 | 17-13-0 | 56.7% | +8.1% |

- **Coverage:** opening lines exist only from late 2023 (434 of 1,440 early home games have an open and a close).
- **Verdict:** no edge. Live it is paper-traded for the rest of the season; the poller checks 5-15 minutes before each qualifying tip.

## 8. Alternate / first-half status (G2, #257)

- **Available:** SharpAPI's free plan lists DK/FD alternate spreads and totals and first-half markets (probe-env, 2026-10-29).
- **Built:** alternate rungs are priced from the engine's simulated margin and (new, storage-only) total histograms, at each NFL Pinnacle board, as a paper signal.
- **First-half rungs: skipped.** The engine does not record half-time scores (BACKLOG).

## 9. Quota (30-day proof with every new poll, `reports/m18/quota_30d.txt`)

| Source | 30-day projection | Limit | Verdict |
|---|---|---|---|
| The Odds API | hourly 165 + poller 261 = 426 credits | 450 usable (500 − 10%) | OK, with a live reserve |
| SharpAPI | poller 39,570 + hourly 958 calls (peak day 2,714) | no daily cap; 12/min | OK: ≤ 10/min by spacing |
| Kalshi | 9,696 | public, ≤ 1/s | OK |
| ESPN (status reads) | 4,248 | ≤ 1/s | OK |
| NBA injury PDFs | 15,120 | ≤ 1/s | OK |
| OddsPapi, Open-Meteo | unchanged | | OK |

The Odds API is the tight one (426 of 450). It is why props from it and the score cross-check stay off.

## 10. Live proof

**Value section, two consecutive automatic hourly runs** (clock-dispatched):

| Run (UTC) | Actions run | Value rows in `status.json` | Proof |
|---|---|---|---|
| 21:00 | 40953944410 | `value_sections: {"nba": 9, "nfl": 0}` | verify-page 40954949898, 21:07: `nba_value_phone.png`, `nfl_value_phone.png` (390 px) |
| 22:00 | 40962384469 | `value_sections: {"nba": 11, "nfl": 0}` | verify-page 40962956226, 22:04, on the `proof` branch |

- **The 20:00 run:** the first to carry the section's code, and it dropped the section. Its read-only poller copy predated the new tables (`no such table: consensus_grades`). Fixed in #259; the page was otherwise fine.
- **The 390 px screenshot of the NBA Value section** shows the live F snipes, each with its pick, price, book, fair vs no-vig and the model's median, and no horizontal scroll. The NFL one shows the empty C-NFL and G2 signals.

**First real NBA alerts graded against their consensus close** (poller job 00:00 UTC Oct 30; close = the 23:54 UTC poll before the 00:00 tips):

| Alert | Pick | Book no-vig at alert | Consensus close | CLV |
|---|---|---|---|---|
| F #7 | De'Andre Hunter u1.5 ast +123 DK | 42.0% | 43.9% (median, two-book) | +1.86 pp |
| F #8 | Precious Achiuwa o1.5 ast +126 DK | 41.5% | 39.2% | −2.25 pp |
| F #9 | DeAndre Jordan o4.5 reb +118 FD | 42.8% | 51.0% | **+8.21 pp** |
| F #18 | Kingston Flemings o3.5 ast +127 DK | 41.3% | 41.4% | +0.16 pp |
| F #19 | Jaylon Tyson u3.5 reb +107 DK | 45.3% | 42.5% | −2.85 pp |
| F #20 | Luguentz Dort o1.5 ast +159 DK | 36.2% | 35.1% | −1.18 pp |
| G1 #21 | SAC @ NOP u234.5 −108 DK | 50.4% | 49.8% | −0.65 pp |

Results at the alerted price are filled in by the M16 grader once box scores land.

**First live C-NBA alert** (#17, 21:48 UTC):
- **The alert:** CHA @ WAS, CHA +1.5 −108 at FanDuel, after Dennis Schröder was listed Questionable (report 5:45 PM ET).
- **The numbers:** fair 54.1% vs FD no-vig 49.6% (edge 4.5 pp, two-book).
- **The weakness it exposes:** the model moved only 0.1 points, so the edge is FanDuel vs the DK/FD median rather than the news. The fixed rule allows this; a post-test rule is in BACKLOG, with no change during the test.
- **Grading:** its game tips Oct 30 23:00 UTC, so it is graded after that.

**C-NFL:** 17 NFL status reads in the CAR @ GB window (Thursday 8:15 PM ET), none qualifying. No NFL value alert; the lag study has 0 events.


## 11. Issues and PRs

**PRs (all merged):**

| PR | What |
|---|---|
| #245 | MODEL.md M18 rules and decision row |
| #246 | A: model picks research only; agreement = Pinnacle + Kalshi |
| #247 | E1: openers, sweeps, change-only archive |
| #248 | E2: no ESPN odds/props; props from the archive |
| #249 | B: consensus, poll marks, removal rows; Odds API props measured |
| #250 | D: medians, quantiles; median-anchored variant and backtest |
| #251 | C-NBA: the value signal |
| #252 | H: consensus-close grading, gates |
| #253 | Page: the Value section |
| #254 | F: opening-line snipes |
| #255 | C-NFL: windows, lag study |
| #256 | G1 |
| #257 | G2 |
| #258 | Replay verification |
| #259 | Value section on a pre-M18 poller DB (found live) |
| #260 | This report, results files, BACKLOG |

**Issues:** #231-#244 are closed with summaries. M16's #166 (its own lag study) is unrelated and stays as it was.

## 12. BACKLOG added

- **NFL engine repricing for news:** only m13w and nflp15 are wired.
- **G2 first-half rungs:** the engine has no half-time score.
- **ESPN schedules, scores and box scores:** the owner decision, with nba_api and nflverse as free replacements.
- **The Odds API score cross-check:** deferred on budget.

## 13. Honest assessment

**What M18 changes:**
- the system no longer bets that its models beat the closing line, which every earlier phase showed they don't;
- it now has an honest answer key: a consensus with stored inputs, graded at the close.

**What is not known:** whether stale-price alerts have value.
- **The replay suggests a small edge:** +0.5 pp CLV on 17 alerts, with a range that includes zero. That is far below M16's +4 pp on 6 alerts, which was graded against DK's own close, a softer key.
- **The binding constraint is free data.** Pinnacle is fresh only at a few boards a day, so most consensus prices are two-book DK/FD medians. That means a higher bar and a softer answer key.
- **F will dominate the alert count.** Nine snipes in its first sweep is a lot for a 6 pp edge on a model whose Brier score only ties the line. Expect its early read (at 60) to be the first verdict, and do not be surprised if it is negative.
- **G1 already looks dead on the backtest.**

**Account-limit risk:**
- The Odds API sits at 426/450 projected. One extra board a day would break it, and the poller's reserve logic is the only guard.
- SharpAPI's free plan has no stated daily cap, but the poller now makes about 1,300 calls a day. If SharpAPI adds a cap or throttles, the sweeps, the archive, props on the page and every DK/FD-based signal stop together. That is a single point of failure.
- The poller runs nearly 18 hours a day on Actions minutes.
