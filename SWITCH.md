# SWITCH.md — the two official NBA cards, and how to retire either

Since the owner decision of 2026-11-05 the NBA tab has **two official cards**, side by side
(MODEL.md "Two official NBA cards"; PROJECT.md decision row of 2026-11-05):

| Card | What it is | Stored as | Record |
|---|---|---|---|
| **Next Gen** | the NBA engine's top 5 by EV still Live at 6:15 AM ET (TAKE when EV ≥ 1.5 × θ) | `card:nba:<day>`, `ledger/card.csv` | from Nov 5 |
| **OG** | Card v1: top 5 by EV among the Live F/C-NBA candidates (M18 TAKE/LEAN) | `shadow:nba:<day>`, `ledger/shadow_card.csv` | Card v1 (Oct 31 – Nov 4) + every OG card from Nov 6 (Nov 5 excluded) |

Both are chosen at 6:15 AM ET by the same "when" rule, both are in the one morning push, and
each has its own record and success goal. Names and rules are in `config/cards.yaml`. No switch
or revert is needed: both run.

## Comparing them

The Research tab's "Next Gen vs OG" table (and the weekly report) compares them over the same
nights only. Its rule (MODEL.md, fixed 2026-11-05): nothing is compared until both have ≥ 200
graded picks; OG is "ahead" only if its EV at close is higher with non-overlapping 95% ranges
and its units are positive. The table informs the owner; it changes nothing by itself.

## Retiring a card later (owner decision only; one PR, CI green)

1. Add a PROJECT.md decision row with the date and the card retired.
2. In `config/cards.yaml`, set `active: false` on the card (`nextgen` or `og`). Merge before
   6:00 AM ET for it to apply to that day's 6:15 selection.

What that does, with no other edit:
- no new card of that kind is selected (`alerts/card.py` `run` for Next Gen, `run_shadow` for
  OG); the morning push says "Today: retired (no new card)" under it;
- its record, goal, Results by day and CSVs stay on the page under the toggle, frozen as they
  were; nothing in the ledger changes (append-only);
- the other card is unchanged.

Optionally, once a card is retired:
- **OG retired:** F and C-NBA could stop generating candidates (add `"snipe"` to `RETIRED` in
  `alerts/poller.py`; skip `signal_value` for the NBA in `Poller.nba_game`) — a separate PR.
- **Next Gen retired:** the engine keeps running as research (Research tab, every prediction
  graded) unless the owner decides otherwise.
- To drop the toggle entirely, set `default:` to the remaining card and approve a UI-freeze
  change that removes the toggle and the retired card's sections.

## History

- 2026-11-03: written as the plan to move the NBA card to the engine (gate: 200 engine picks
  passing the three checks).
- 2026-11-04: executed by owner override (the engine card from Nov 5; Card v1 archived).
- 2026-11-05: Card v1 kept as a shadow, then made official again as "OG" beside "Next Gen".
