"""Tests for util/timing.py (node 5agg / PERF-F3)."""

from __future__ import annotations

from gitbulk.util.timing import PhaseTimer


class _FakeClock:
    """Deterministic monotonic clock: each call returns the next queued tick."""

    def __init__(self, ticks: list[float]) -> None:
        self._ticks = list(ticks)

    def __call__(self) -> float:
        return self._ticks.pop(0)


def test_mark_records_elapsed_since_construction():
    clock = _FakeClock([100.0, 100.5])  # construct, then mark
    timer = PhaseTimer(clock=clock)
    timer.mark("preflight")
    assert timer.timings == {"preflight": 0.5}


def test_sequential_marks_measure_disjoint_spans():
    clock = _FakeClock([0.0, 1.0, 3.5, 4.0])  # construct + 3 marks
    timer = PhaseTimer(clock=clock)
    timer.mark("preflight")  # 1.0 - 0.0
    timer.mark("per_repo")  # 3.5 - 1.0
    timer.mark("per_pr")  # 4.0 - 3.5
    assert timer.timings == {"preflight": 1.0, "per_repo": 2.5, "per_pr": 0.5}


def test_remarking_same_name_accumulates():
    clock = _FakeClock([0.0, 2.0, 5.0])  # construct + 2 marks of same name
    timer = PhaseTimer(clock=clock)
    timer.mark("loop")  # 2.0
    timer.mark("loop")  # 3.0 → accumulates to 5.0
    assert timer.timings == {"loop": 5.0}


def test_default_clock_is_monotonic_and_nonnegative():
    timer = PhaseTimer()
    timer.mark("phase")
    assert timer.timings["phase"] >= 0.0
