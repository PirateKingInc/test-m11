"""M15 NFL side-variable groups, in the order selection adds them (MODEL.md "M15 NFL side
variables"). Kept apart from groups.py so the M12/m13w group list is unchanged."""

from __future__ import annotations

from betdata.nfl.features.m15_depth import Depth
from betdata.nfl.features.m15_lines import Lines

GROUPS15 = {g.name: g for g in (Lines(), Depth())}
ORDER15 = list(GROUPS15)
