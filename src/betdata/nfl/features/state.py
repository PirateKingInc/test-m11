"""Incremental state over a frame's known prefix.

Frames only grow at the end, so state built from the first n rows can be advanced to n' > n
by feeding rows n..n'. A request for fewer rows than the state has seen rebuilds it from
scratch, so the result is always a pure function of the known prefix (harness-safe).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from betdata.nfl.store import AsOf


class Rolling:
    """Base class: subclasses set `frame` and implement `step(row)`."""

    frame: str = ""

    def __init__(self):
        self.n = 0

    def step(self, row) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def advanced(a: AsOf, key: str, make: Callable[[], Rolling]) -> Any:
    n = a.n(make.frame if hasattr(make, "frame") else make().frame)
    st = a.store.cache.get(key)
    if st is None or st.n > n:
        st = make()
    if st.n < n:
        df = a.store.frame(st.frame).df
        for row in df.iloc[st.n : n].itertuples(index=False):
            st.step(row)
        st.n = n
    a.store.cache[key] = st
    return st
