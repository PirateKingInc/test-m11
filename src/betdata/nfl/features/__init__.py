"""Feature groups. Each group's `compute(a, g, point)` returns a dict of features for game g
as seen at `a.t`. Names are `m_<group>_<name>` (margin model, home minus away) or
`t_<group>_<name>` (total model). Every group must pass the as-of harness (nfl/asof.py)."""
