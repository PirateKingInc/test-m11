# M16 final report: alerts, the lag study and closing-line value

Written 2026-10-27. All numbers come from the live state (the hourly database and the
poller's own `data/poller.sqlite`), read through the `probe-env` workflow. They cover
2026-10-23 to 2026-10-27 16:00 UTC unless stated.

M16 adds a layer on top of the hourly page:
- a fast injury poller on the clock workflow;
- two alert signals;
- ntfy pushes, each alert stored as a paper bet;
- a lag study;
- closing-line grading;
- an Alerts section on the page.

No model version, frozen fit or existing migration changed. The hourly page is
byte-identical except for the Alerts section.

## 1. Prop audit (Part A1, #155, PR #172)

The M15 NBA prop backtest showed 30-39% win rates with ROIs of only −3% to −18%.

- **The arithmetic was right.** There were no pushes or voids, but the bets were long prices (median +175 to +200).
- **Cause: the wrong lines.** ESPN labels alternate ladder lines too, and the parser took the first labelled over/under pair as the main line. So 4-31% of history rows (by season and book) were alternates priced like +300/−450.
- **Fixes:**
  - The parser now takes the most balanced pair.
  - The backtest keeps only lines with a no-vig over of 0.35-0.65.
  - The frozen nbap15 was re-scored, not refitted or re-tuned.

| Market | Bets (W-L) | ROI before | ROI re-scored |
|---|---|---|---|
| points | 744-1,092 | −18.2% | −10.7% |
| rebounds | 1,011-980 | −14.2% | +10.6% |
| assists | 1,443-1,593 | −7.9% | +5.0% |
| threes | 3,348-3,921 | −3.2% | +0.9% |

Notes on the re-score:
- The model's Brier score is still no better than the market's.
- The rebounds edge is almost all unders, at opening lines.
- The fit, calibration and flags were left unchanged: M16 forbids page changes. NBA props stay information only.

## 2. Matcher fix (Part A2, #156, PR #173; live repair in M15.1)

Two real games between the same teams within 36 hours (a two-game series) were merged into one game.

- **Fix.** An exact source id wins. Otherwise the closest kickoff is matched, and a game already holding another event of the same source is never matched. A re-listed id within 3 hours still matches its game.
- **Evidence.** On 2020-21 to 2024-25 data there were 55 merged games before the fix and 0 after. Fixture tests cover the series case.
- **Live repair.** The `nba-split-merged` workflow split the 89 merged games in the hourly state; none had odds rows to move. It then reloaded 2018-2025 results, box scores and stints. A re-check found 0 merged games left.

## 3. Sources and free-plan features (Part B, #158-#162)

| Source | Pinnacle | Props | Cost | Verdict |
|---|---|---|---|---|
| SharpAPI | no (DK + FD only) | yes (points, rebounds, assists, made threes; NFL receiving and rushing yards, receptions) | 12 requests a minute, 60 s delay, 200 rows a call | **used**. It also exposed a live hourly bug: alternate lines stored as market lines (fixed, #176) |
| The Odds API | **yes** (`bookmakers=pinnacle`) | yes, but 1 credit per market per event | 1 credit per market (3 a board), 500 a month | **used** for Signal 2, inside a live reserve for the hourly job |
| SportsGameOdds | **no** (not in the free payload) | yes | 2,500 objects a month | **dropped** |
| Kalshi | - | - | free, no key | **used** as a secondary probability in the lag study |

**Quota proof** (30 days on the live schedule, `betdata alerts quota`):
- The Odds API: hourly 156 + poller 249 = **405 of 450** usable credits.
- SharpAPI: at most 10 calls a minute (the limit is 12), and none while an hourly run is in progress.
- Keyless sources: at most 1 request a second.

**Live spend so far:**
- The Odds API: 24 credits on 8 boards.
- SharpAPI: about 300-550 calls a day on game days (largest day: 548 calls on 10-25).

## 4. Poller: health and timing (Part C, #163)

- **Where it runs.** The `poll` job of the clock workflow, each hour until :56, on a throwaway copy of the hourly state.
- **Lock.** It skips any minute while an hourly run is queued or in progress.
- **Record.** 128 jobs, 0 failed, 2,612 cycles, 180 minutes skipped for the hourly run. The hourly page ran every hour on time throughout.

Per day (UTC). Quotes are counted in rows; "graded" means graded against the pre-game close.

| Day | Jobs | Report requests | Status changes (qualifying) | Re-predictions | SharpAPI DK / FD quotes | Pinnacle quotes | Kalshi quotes | Alerts created / pushed / graded |
|---|---|---|---|---|---|---|---|---|
| 10-23 | 26 | 55 | 54 (4) | 3 | 726 / 528 | 144 | 86 | 0 |
| 10-24 | 28 | 58 | 358 (28) | 18 | 15,982 / 14,760 | 96 | 564 | 0 |
| 10-25 | 28 | 68 | 641 (50) | 35 | 23,958 / 23,068 | 374 | 1,050 | 4 / 4 / 4 |
| 10-26 | 26 | 65 | 193 (24) | 17 | 14,534 / 13,735 | 54 | 484 | 2 / 2 / 2 |
| 10-27 (to 16:00) | 19 | 21 | 24 (1) | 1 | 456 / 477 | 0 | 52 | 0 |

The 10-25 and 10-26 rows include NFL: 196 and 12 status changes, 21 and 2 qualifying.

- **Timing.** A report is read within one 3-minute cycle of publication: every alert came 3 minutes after its report's stated time.
- **Request volume.** About one report request per published report, since the poller asks only for names newer than the last one read.
- **Volume check.** Qualifying changes are not implausibly low: about 4-30 a day on the NBA, against 75-145 injury-report rows a day stored by the hourly job.

**A real bug found and fixed (REVIEW M16-4, PR #185).**
- **What happened.** Each job's fresh state copy lacked the reports the previous hour's job had read, so the next report repeated a change already handled.
  - 25 of 107 qualifying events were repeats.
  - 2 of the 6 alerts were duplicates: Edgecombe, and Zubac/Nembhard.
- **Fix.** The poller's own last record per (game, player) now decides whether a change is new.
- **Proof.** A replay on a copy of the live state used the real 2026-10-25 DET@PHI sequence, with frozen nba15, the DK/FD quotes stored that evening, and a mock ntfy.
  - Job 1 found VJ Edgecombe (34.9 minutes per game played) Questionable → Out (0.50 → 0.005).
  - It re-predicted only DET @ PHI: home margin −7.1 → −8.1.
  - It alerted once: **DET −1.5 at −105, FanDuel, edge 6.6 points**.
  - Job 2, from a fresh copy, saw the same change on the 7:00 PM report and recorded nothing.

The earlier replay on dev data is in PROJECT.md "M16 replay" (Mobley, WAS @ CLE).

**Day-before changes.** Changes reported a day before tip often have no DK/FD quote. SharpAPI lists a game only once books post its line; on 10-27 it listed 1 of 8 games for 10-28. These events have no lag data for those books and are left out of the medians.

## 5. Signals and alerts (Parts D, #164, #165)

### Signal 1 (news)

- 107 qualifying changes were recorded (84 NBA, 23 NFL). 82 are distinct after removing the repeats above.
- 74 re-predictions ran (61 NBA, 13 NFL).
- 6 NBA alerts were created and all 6 pushed (ntfy HTTP 200): 4 distinct changes plus the 2 duplicates.
- No NFL change cleared the side or prop thresholds.

### Signal 2 (soft vs sharp)

It is live: DK/FD are compared with Pinnacle via The Odds API.

- 12 boards ran (7 NBA, 5 NFL).
- 317 market pairs were compared at the same line; 196 were skipped because the lines differed.
- The largest no-vig gap seen was **2.86 pp**, under the fixed 3.0 pp threshold, so there are **0 Signal 2 alerts**, correctly.

### Dedup, quiet hours and paper bets

- One alert per (game, market, side, signal, triggering change); fixed live as above.
- Quiet hours are 23:00-07:00 ET: alerts are stored, not pushed.
- Every alert is a 1-unit paper bet.
- All of this is covered by `tests/alerts/test_poller.py`.

## 6. Alerts and their CLV so far (Part F, #167, #168)

`betdata grade alerts`, all 6 alerts graded against the same book's last pre-game line:

| Signal | n | CLV points | CLV no-vig (95% range) | W-L | ROI |
|---|---|---|---|---|---|
| news, NBA spreads | 6 | +1.17 | +4.07 pp [+1.93, +6.21] | 6-0 | +90.2% |

Without the 2 duplicates there are 4 distinct alerts, with mean no-vig CLV +4.84 pp, 4-0.

**This is no verdict.** The success rule needs 200 graded alerts per signal; 6 is noise as much as signal. The page's Alerts section shows these numbers, labelled "Paper alerts, not advice", with the 200-alert caveat.

## 7. Lag study so far (Part E, #166)

`betdata alerts lag`, 107 qualifying changes with a closed 60-minute window (study start 2026-10-23 21:03 UTC):

| Book | Lags | Median | Under 5 min | Censored at 60 |
|---|---|---|---|---|
| DraftKings | 73 | 12.0 min | 16 | 19 |
| FanDuel | 73 | 48.0 min | 14 | 33 |
| Pinnacle | 17 | 45.0 min | 2 | 7 |
| **DK + FD pooled** | 146 | **21.0 min** | 30 | 52 |

**Signal 1 is go, not yet decided.** The fixed rule turns Signal 1 off only if the pooled soft-book median lag is under 5 minutes; so far it is 21 minutes.

- The decision is taken once, automatically, by the first poller job after 2026-11-06 21:03 UTC.
- Caveats on the current figures:
  - They include the 25 repeated events from before the fix.
  - Pinnacle lags are few, because its quotes come only from the budgeted boards.

## 8. Verification

| Item | Result |
|---|---|
| (a) full regression | 513 tests pass on main; CI green. No model version, fit or migration changed. With the Alerts step removed, the rest of the page is byte-identical (test). |
| (b) failure isolation | The poll job is separate from the clock's `tick`; the hourly page only reads the poller DB, in `continue-on-error` steps. A missing or corrupt poller DB gives "Alerts unavailable" (test). Live: 128 poll jobs, 0 failures, an hourly run every hour. No fault was injected live. |
| (c) replay | Dev data: Mobley Questionable → Out, one cycle, one game re-predicted. Live-state replay: Edgecombe, one alert, no repeat (above). |
| (d) dedup and quiet hours | Tests pass. The live dedup bug was found and fixed (#185). |
| (e) 30-day quota | 405 of 450 Odds API credits; SharpAPI at most 10 a minute (above). |
| (f) live proof | 6 real ntfy alerts delivered (HTTP 200) and stored. The lag study has 5 days of data. |
| line storage (#157) | Every game since live polling began has opening, current and pre-game close lines. For NFL games 10-24 to 10-27, all 13 have them at every book. Earlier gaps come from polling start dates. |

## 9. Honest assessment

- **The machinery works end to end on free data.** Reports are read within minutes, the frozen models re-predict only the affected game, alerts reach the phone, and every alert is graded at the close.
- **The first 6 alerts beat the close.** But they are 4 distinct changes in 5 days. The 200-alert rule will take months at this rate, and the +4 pp CLV must be read as anecdote.
- **The lag study's early answer favours Signal 1.** DraftKings typically moves about 12 minutes after a report and FanDuel much later. A 3-minute poll plus SharpAPI's 60-second delay is fast enough if this holds; the decision on 11-06 settles it.
- **Signal 2 is starved by the free plan.** Two NBA boards a day and one per NFL kickoff window is all The Odds API budget allows. In 12 boards no soft price was 3 pp off Pinnacle. Expect it to alert rarely.
- **NFL detection is live (23 qualifying changes) but has produced no alert.** NFL prop grading needs nflverse files the poller job does not have, so NFL prop alerts would stay ungraded until that is addressed.
