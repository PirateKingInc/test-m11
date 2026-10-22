"""Grading alerts against the pre-game close (MODEL.md "M16 / Grading", issue #167).

`grade_all` is called once at the start of every poller job. Until #167 lands it grades
nothing: alerts stay stored as ungraded paper bets, and no grade is invented.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def grade_all(main, pdb, now) -> int:
    """Grade every alert whose game has started; returns the number graded."""
    log.info("alert grading arrives with #167; nothing graded")
    return 0
