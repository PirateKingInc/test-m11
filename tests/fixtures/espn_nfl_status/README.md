Hand-built ESPN NFL fixtures for `tests/alerts/test_nfl.py` (M16 NFL status poller).

This sandbox cannot reach ESPN, so these files were written by hand in the shapes verified live
from GitHub Actions on 2026-10-22:

- `summary_*.json`: `site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event=<id>`,
  trimmed to `header.competitions[].competitors[].team` and `injuries` (one block per team:
  `{team: {id, abbreviation}, injuries: [{status, date, type, details, athlete: {id,
  displayName, position}}]}`).
- `roster_*.json`: `sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/<id>/
  competitions/<id>/competitors/<teamId>/roster`, `entries: [{playerId, active, starter,
  didNotPlay, displayName, position: {$ref}}]`. The `*_released` files have >= 40 active
  entries (the inactive list is out); `roster_buf_early.json` has 10 (before the release, when
  `active` is unreliable).
- `scoreboard_20261025.json`: `site.api.espn.com/.../nfl/scoreboard?dates=YYYYMMDD`, one event.

Players, ids, statuses and dates are illustrative, not real reports.
