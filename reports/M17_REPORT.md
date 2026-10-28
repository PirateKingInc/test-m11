# M17 final report: a market-free NFL engine

Written 2026-10-28. M17 built an NFL engine bottom-up: plays → pass/run → dropbacks →
targets and carries → efficiency → drives → 10,000 simulations per game. No betting line is
an input. It ships as two new versions, beside m12/m13w/m15/nflp15, with nothing existing
changed:
- `nfle17` (sides);
- `nflpe17` (props).

Untouched: model versions, frozen fits, the alerts layer, the append-only odds history and
existing migrations.

**Bottom line:**
- The engine is a sound simulator. Its volumes are within 3-5% and it beats nflp15 on prop MAE.
- It does not find what the market misses:
  - its sides were worse than m13w;
  - its validated side threshold lost on the test seasons;
  - the closing line beats its props in every market.
- So it runs live as **info only**: no side or prop flags, and the page stays on m13w.

## 1. Part A: measure first

**A1 stability** (`reports/nfl/stability.csv`; training 2016-2019; split-half r, padding):
- **Volume is stable:** target share r 0.91 (padding 23 targets), carry share 0.95 (7 carries), aDOT 0.89.
- **Efficiency is mostly noise:** yards per target 0.26 (108 targets), receiving TD per target 0.13 (269), EPA per designed run 0.24 (635), turnovers per drive 0.18.
- **Defences barely control sacks** (0.08, padding 3,939) but do control pressure (0.50).
- **Pace is offensive only:** a defence's seconds per play allowed has r −0.01, so it is dropped.
- **Team rates regress hard season to season** (r 0.2-0.4).

**Two rules fixed before any result:**
- "Neutral" plays: score within 10, downs 1-2, outside the last two minutes, regulation.
- No line-derived inputs: nflfastR `xpass`/`pass_oe`/`vegas_wp` are never read, so the engine fits its own pass curve.

**A2 data timing and routes:**
- Every row carries `known_at`; a game's plays are known at kickoff + 12 h.
- Participation (routes, pressure) is published after the season, and 2026 is not out.
- On validation, **target share is as accurate as real routes** (MAE 1.750 vs 1.748). The snap-share route proxy is 7% worse (1.873). So step 4 uses target share and needs no participation data live.

**A3 pressure flags:** NGS participation and PFR agree at team-game r **0.41**, and their levels drift apart from 2023. That is below the fixed 0.7 rule, so pressure is **not used**. The sack step uses shrunk offence, QB and defence sack rates.

**A4 coordinators** (`seeds/coordinators_nfl.csv`; Wikipedia via the MediaWiki API, CC BY-SA 4.0, revision per row; no Pro-Football-Reference):
- **Coverage:** 352 team-seasons. Head coach all 352; OC 301; DC 300. A role is left empty rather than guessed.
- **Spot-checks:** 38/40 matched automatically; the other 2 were confirmed by hand.
- **Persistence:** after an OC change, last season's pass rate predicts much less (r 0.46 → 0.35) and so does pace (0.55 → 0.28). The rule is met for OCs; DCs show no effect.
- **Not used in nfle17:** the table was finished after the design froze (BACKLOG, engine v2).

## 2. Parts B-C: the engine and the simulation

`src/betdata/nfl/engine/`:
- `plays` (tables), `rates` (as-of EW rates shrunk with A1 paddings), `avail` (M12 play probabilities);
- `drives` (multinomial-logit drive outcomes, FG curve, templates), `sim` (vectorised drive-by-drive), `inputs` (depth charts as-of);
- `backtest`, `value`, `live`, `page`.

**Simulation** (Part C):
- 10,000 simulations per game, with a correlated game script and a game-level yard split.
- Targets and carries are allocated Dirichlet-multinomial; player yards are rescaled to team totals.
- `consistency()` checks the identities on every run: team = sum of players; points = 7·TD + 3·FG + extras.

Two build bugs (QB order across teams; a run-biased yard split) were fixed and are not counted as adjustments.

**Validation (2020-2022)**, full build plus ≤3 adjustments:

| Variant | Margin MAE | Kept |
|---|---|---|
| full build | 10.063 | baseline |
| 1. half-life 10, last season ×0.7 | **10.049** | yes |
| 2. paddings ×0.5 | 10.061 | no |
| 3. QB weight 1.0 | 10.062 | no |

m13w's validation margin MAE is 9.819.

Departures from the Part B plan are listed in MODEL.md: target share not routes; no aDOT catch curve; no yards-before-contact term; league kicking and punting; rookies from the M12 role.

## 3. Parts D-E: engine vs market

**Sides, test 2023-2025, scored once** (repeat look; nothing changed after):
- **Margin MAE 10.27** vs m13w 10.06/9.99, so the page stays on m13w (M15 rule).
- **Other measures:** total MAE 10.41; Brier 0.223; volume bias within 3% (rushing yards +4.7%).
- **Threshold:** 4.0 points was chosen on validation (+11.5%, 164 picks; the range included zero). On test it went **74-80-3, ROI −8.3%** (95%: −23% to +7%). **No side flags.**

**The three tests:**
1. **Gaps vs results:** slope −0.014 per point (se 0.022). Cover rates are 0.47-0.50 in every band.
2. **Gap at the open vs the move to the close:** +0.044 (se 0.025, n = 761). 53% of moved lines went toward the engine at gaps ≥ 1, and 59% at gaps ≥ 4 (n = 78). Weak evidence that the market drifts toward the engine.
3. **Which steps the market agrees with:** the close prices the efficiency steps at about 0.7-1.4×. No step predicts the cover beyond about 2 se.

**ESPN line history coverage** (games per season with a line):

| Seasons | Close | Open |
|---|---|---|
| 2016-2022 | every game (267-286 a season) | none |
| 2023 | 285 | 192 (from mid-season) |
| 2024 | 284 | 284 |
| 2025 | 285 | 285 |

ESPN keeps no opening lines before late 2023. Projection "books" (consensus, numberfire, teamrankings, accuscore) are excluded.

**Props (nflpe17):**
- **Against nflp15** (test MAE): the engine is better everywhere except anytime TD (even): passing 63.7 vs 69.7, receiving 20.97 vs 21.61, receptions 1.578 vs 1.591, rushing 21.05 vs 21.11.
- **Against the ESPN BET close (2024-2025):** the no-vig line's Brier score and MAE beat the engine in every market.
- **Thresholds** chosen on 2024, scored on 2025:
  - passing yards −6.1%;
  - receptions +1.8% (95%: −4.7% to +8.3%), so info only;
  - receiving and rushing yards: no threshold.

  **No prop flags.**

## 4. Part F: fantasy data (`reports/nfl/m17_fantasy.json`)

| Source | Terms / reach | Use | Result |
|---|---|---|---|
| ff_opportunity | nflverse, reachable | benchmark + input test | as a projection, worse than the engine everywhere. As an input it lowers validation MAE by about 1-2%, which is significant, so it passes the rule. It came after the freeze, so it goes to BACKLOG |
| ff_rankings (FantasyPros ECR) | nflverse, reachable | benchmark only | experts rank better: Spearman 0.44 vs 0.41 on 2023-2025 |
| Sleeper | "free to use for non-commercial purposes" | benchmark only (timestamps missing or post-kickoff) | the engine's median roughly ties it on receiving and trails on volume and rushing |
| ESPN fantasy | Disney Terms of Use forbid automated extraction | **dropped** | – |

**Owner decision needed:** the same Disney clause covers the keyless ESPN endpoints the project has used since M14 (scoreboards, box scores, injuries, prop and line history). Logged in BACKLOG; nothing was changed.

## 5. Part G and live proof

**Page:** the NFL tab has an "Engine vs market" section. Each game shows:
- the engine spread and total with their 10-90% range, the current line, the gap and P(cover);
- the three biggest chain steps;
- props with median, line, P(over) and edge, all marked "info only".

The intro states the test result: no flags, ROI −8.3%. nfle17's picks at 4.0 are graded and shown in their own season record (M15.1 rules: spreads only, no moneyline).

**Two consecutive automatic hourly runs** (clock-dispatched, commit 6dcc86f; the log fields come from #223):

| Run (UTC) | Actions run | Whole run | NFL predict (all versions) | Engine | Engine rows stored (change-only) | Engine section on the page |
|---|---|---|---|---|---|---|
| 11:00 | 40676512518 | 136.9 s | 41.8 s | 24.9 s, 14 games, 164 prop lines | 7 game rows, 140 prop rows | 14 games |
| 12:00 | 40684272658 | 135.1 s | 68.0 s | 43.9 s, 14 games, 164 prop lines | 2 game rows, 87 prop rows | 14 games |

- **Runtime (C2):** both runs finished within 3 minutes, including every sport, version and fetch. The engine took 25-44 s of it at 10,000 simulations a game.
- **Change-only storage:** a game's row is written only when its prediction changes. Hence 7, then 2.
- **Deploys:** both Pages deployments reported success.
- **Phone screenshot (390 px):** the `verify-page` workflow fetched the live page from outside, took `nfl_engine_phone.png` and pushed it to the `proof` branch.

**The screenshot caught one bug,** fixed in #226. Engine props were listed under ESPN athlete IDs, because ESPN's NFL prop lines carry no name. Names now come from the stored rosters, and rows already stored are shown with the roster name of their gsis id. Everything else on the 12:28 UTC screenshot was as designed:
- the info-only intro with the −8.3% test ROI;
- CAR @ GB: engine GB −5.4 (10-90%: GB −21.0 … CAR −10.0) against market GB −3.5, a 1.9-point gap toward GB, P(cover GB) 57%;
- total 50.4 against 47.5;
- biggest steps: passing offence, home field, pass defence;
- props with median, line and P(over), each marked "info only".

The page had 14 engine game cards, with no horizontal scroll at 390 px.

**After the fix.** The 13:00 UTC automatic run deployed #226 (run 40692499185, commit d4c96b0), and verify-page was re-run at 13:25. All 164 engine prop rows on the live page now show player names (Jordan Love, Josh Jacobs, Bryce Young, …), with none showing an ID. The screenshot is in `reports/nfl/m17_engine_phone.png`.

## 6. Issues and PRs

**PRs (all merged to main):**

| PR | What |
|---|---|
| #209 | MODEL.md M17 plan and issues |
| #210 | fetches: participation, PFR, ESPN lines |
| #211 | NBA prop flags: threes info only |
| #212 | A1 stability, A2 routes, A3 pressure |
| #213 | Wikimedia user agent |
| #214 | staff-list fallback |
| #215 | rates, availability, drive model |
| #216 | simulation, inputs |
| #217 | value layer, backtest, lines, props evaluation |
| #218 | strict staff-list parser |
| #219 | live nfle17/nflpe17, grading, validation section |
| #220 | page section, design, live fit |
| #221 | coordinator table, Sleeper fetch |
| #222 | test results; no flags |
| #223 | run-log proof of engine rows, runtime and page section |
| #224 | Part F benchmarks; BACKLOG; decision row |
| #225 | verify-page engine screenshot |
| #226 | player names on engine props (found by the screenshot) |
| #227 | this report |

**Issues:**
- #194-#199, #201, #202 and #205 are closed with summaries.
- #200 (C2 runtime), #206 (live), #207 (page) and #208 (verification) close with the live proof below.
- M16's #166 (lag study) is separate and stays open: it accumulates through the season.

## 7. BACKLOG added

- **Engine v2 inputs** that passed their rule after the freeze: the coordinator prior and ff_opportunity. Judge them on 2026.
- **Kicking and punting terms.**
- **Prop volume.** FantasyPros and Sleeper both beat the engine there, and the line beats the engine's props.
- **nfle17 live CLV** to track through 2026.
- **The ESPN terms question.**
- **Fit-file size:** 2.9 MB of JSON.

## 8. Honest assessment

The engine does what a play-level simulator should. Its volumes, totals and identities hold, and on player volume it is better than this project's earlier prop model. As a betting tool it found nothing:
- the side edge seen on validation reversed on the test;
- the market already prices every chain step the engine measures;
- the closing prop line is more accurate than the engine in every market.

The one positive sign is weak: test 2 suggests the market drifts slightly toward the engine between open and close (about 1.8 se). That is worth tracking live, not betting on.

**Where the next gains are, in order:**
1. **Player volume:** expert rankings and Sleeper beat the engine there.
2. **The coordinator prior.**
3. **ff_opportunity.**

All three are inputs, not structure. The engine itself is reusable as is.
