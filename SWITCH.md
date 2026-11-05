# SWITCH.md — moving the NBA card to the M20 engine

**Executed 2026-11-04 by owner override** (the gate below was waived), effective with the
2026-11-05 8:50 AM ET card: see MODEL.md "Engine card (owner override 2026-11-04)" and the
PROJECT.md decision row. Differences from the plan below: the engine label is TAKE when EV ≥
1.5 × θ (no news/Pinnacle condition, the owner's rule); nba15/nbap15 hourly predictions keep
running (no picks; the frozen sport tab's sections keep their data); the engine card is chosen
once at 8:50 even when empty.

---

(The original plan, written 2026-11-03:)

Written 2026-11-03 with M20. Nothing here has been done. The card keeps coming from the existing
NBA picks (C-NBA value alerts and F snipes) until the owner approves this switch in writing (an
issue comment or a PROJECT.md decision row).

## When to consider it

Only after the research signal `nba-engine` **works** under the M20 rule (MODEL.md "M20: Part A"):
all three checks pass over ≥ 200 graded engine picks —
1. calibration (gap not significantly above 0, ECE ≤ 0.05),
2. Brier score closer than the closing no-vig line (whole 95% range below 0),
3. EV at the close above 0 (whole 95% range above 0).

The Research tab's "NBA engine" section and `betdata alerts checks` show the status. If the
engine only matches the market (checks 2-3 fail), do not switch: the card would trade one
losing-after-margin signal for another.

## The switch, step by step (one PR, reviewed, CI green)

1. **Decision row.** Add a PROJECT.md decision row: "Card switches to nba-engine (owner, date)".
2. **Labels.** In `src/betdata/alerts/labels.py`:
   - add `"engine"` to `SIGNALS` (this alone puts engine picks into the card pool, the TAKE/LEAN
     labels and the sport tab's Today list — the tracker and card both filter on `SIGNALS`);
   - in `edge_and_threshold`, return `(details["ev"], details["theta"])` for `signal ==
     "engine"`, so TAKE = EV ≥ 1.5 × θ AND (news trigger OR a fresh Pinnacle price sides with
     the pick), the existing TAKE rule.
   - Picks created before the switch stay unlabelled (no backfill), as in M18.
3. **Card pool.** For the NBA only, remove `"value"` and `"snipe"` from the card's pool in
   `alerts/card.py` (`alerts.c.signal.in_(...)` for `sport == "nba"`), so the NBA card is the
   top 5 engine picks by EV at 8:50 AM ET. EV is already the engine's fair × decimal − 1.
   NFL is untouched.
4. **Push.** The card push (one ntfy message per sport) is unchanged; individual engine picks
   stay unpushed (they are recorded with `notify.record`, never `emit`).
5. **Main page.** No structural change is needed: card picks render with the existing row
   format, so `tests/runner/test_ui_freeze.py` must still pass with the snapshot unchanged.
   If it fails, the switch PR is wrong.

## What gets archived (code and history kept; nothing deleted)

| Piece | Today | After the switch |
|---|---|---|
| `nba15` sides (`nba/live15.py`, `config/fits/nba15_fit_2015_2025.json`) | predicted hourly, Research tab, reprices C-NBA | stop `NbaSport.predict_15` in the hourly run; keep the fit and stored predictions; Research tab keeps its season record |
| `nbap15` props (`nba/props/`, `config/fits/nba_props_2015_2025.json`) | predicted hourly, prices F snipes and C-NBA props | stop `NbaSport.predict_props`; keep the fit, stored predictions and grades |
| M14 `nba` (`nba/live.py`) | background version | stop `run_predict` (it already runs in the background only) |
| F (opening-line snipes, `alerts/snipe.py`) | creates picks on new DK/FD rebound/assist lines | add `"snipe"` to the poller's `RETIRED` set (as M20 did for G1/G2); its ledger history and records stay |
| C-NBA (value after news, `alerts/value.py`) | creates picks after qualifying NBA status changes | set the existing kill switch `signal1:off` for NBA, or add an NBA guard in `Poller.nba_game`; the engine's own injury trigger (`engine_picks(..., resim=True)`) replaces it |

C-NFL, G1/G2 history and every NFL model are untouched.

## Rollback

Revert the switch PR. Picks created under the switch stay in the ledger (append-only) with their
labels; the official record keeps them as they were graded.

## Checks after the switch

- Two consecutive automatic hourly runs: the NBA card section shows engine picks, the main page
  structure test passes, the Research tab still shows the engine's three checks.
- `ledger/card.csv` rows for the next card date name `nba-engine` picks only (NBA).
- The weekly report's official record covers the new card picks from the switch date.
