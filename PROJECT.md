# Project decisions log

Decisions that change dependencies, data rules or scope, newest last.

| Date | Decision | Reason |
|---|---|---|
| 2026-10-19 | M12 starts: NFL model with as-of data, two prediction points per game (`final_report`, `prekick`). | See MODEL.md. |
| 2026-10-19 | Injury `date_modified` is stored in `injuries.report_date`; existing rows are filled from archived raw files. | It is the only publish timestamp nflverse gives; needed for as-of injury data (REVIEW.md #3). |
