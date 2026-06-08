"""Lightweight wall-clock phase timing for command pipelines.

A :class:`PhaseTimer` records the elapsed seconds between sequential
checkpoints in a command's pipeline (preflight → per-repo → per-PR). The
accumulated mapping is handed to :meth:`gitbulk.runstate.RunState.record_timings`
so per-phase cost lands in the run manifest, giving a cross-run baseline to
confirm a perf fix helped and to catch an O(n^2) regression (node 5agg /
PERF-F3, sibling of the 7gpd write-amplification fix).

``perf_counter`` (monotonic, unaffected by wall-clock adjustments) backs the
measurement; the clock is injectable so tests stay deterministic.
"""

from __future__ import annotations

from time import perf_counter
from typing import Callable


class PhaseTimer:
    """Accumulate wall-clock seconds between sequential pipeline phases.

    A command constructs the timer at the start of its pipeline and calls
    :meth:`mark` at the END of each phase; each call records the elapsed
    seconds since the previous mark (or since construction, for the first).
    The :attr:`timings` mapping is then passed to
    :meth:`RunState.record_timings`::

        timer = PhaseTimer()
        ...preflight work...
        timer.mark("preflight")
        ...per-repo work...
        timer.mark("per_repo")
        ...per-pr work...
        timer.mark("per_pr")
        rs.record_timings(timer.timings)

    Re-marking the same name accumulates (so a name can span several
    non-contiguous spans of a loop); each mark advances the internal cursor
    so phases never double-count the same interval.
    """

    def __init__(self, *, clock: Callable[[], float] = perf_counter) -> None:
        self._clock = clock
        self._last = clock()
        self.timings: dict[str, float] = {}

    def mark(self, name: str) -> None:
        """Record elapsed seconds for phase ``name`` since the previous mark."""
        now = self._clock()
        self.timings[name] = self.timings.get(name, 0.0) + (now - self._last)
        self._last = now
