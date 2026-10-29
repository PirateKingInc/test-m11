"""M18 Part E: no ESPN odds or prop endpoint is called (Disney's Terms of Use forbid automated
extraction; MODEL.md "M18 Part E", BACKLOG "ESPN data"). Every code path that used to fetch
ESPN odds or prop lines calls `refuse` first. The stored history is kept and still read.
ESPN schedules, scores and box scores are an open owner decision and are unchanged."""

from __future__ import annotations

# Only the regression tests of the retired fetch code (which still documents the format of the
# stored history) set this; nothing in the package does.
ALLOWED = False


class EspnOddsDisabled(RuntimeError):
    pass


def refuse(what: str) -> None:
    if ALLOWED:
        return
    raise EspnOddsDisabled(f"{what}: ESPN odds and prop endpoints are not called since M18 "
                           "(MODEL.md 'M18 Part E')")
