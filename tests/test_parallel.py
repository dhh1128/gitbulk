"""Unit tests for :func:`gitbulk.util.parallel.parallel_map`.

The order-preserving bounded thread-pool map used by the prune-branches
scan (node prnpf8nq). No network, no real work — just plain callables.
"""

from __future__ import annotations

import threading

import pytest

from gitbulk.util.parallel import parallel_map


def test_empty_returns_empty_list_without_calling_fn():
    calls = []
    out = parallel_map(lambda x: calls.append(x), [], concurrency=4)
    assert out == []
    assert calls == []


@pytest.mark.parametrize("concurrency", [1, 4])
def test_results_preserve_input_order(concurrency):
    out = parallel_map(lambda x: x * x, [1, 2, 3, 4, 5], concurrency=concurrency)
    assert out == [1, 4, 9, 16, 25]


def test_concurrency_one_runs_inline_in_order():
    seen = []
    out = parallel_map(lambda x: seen.append(x) or x, ["a", "b", "c"], concurrency=1)
    assert out == ["a", "b", "c"]
    # inline path runs strictly in input order
    assert seen == ["a", "b", "c"]


def test_concurrency_zero_or_negative_falls_back_to_inline():
    out = parallel_map(lambda x: x + 1, [1, 2], concurrency=0)
    assert out == [2, 3]


def test_on_progress_called_once_per_item_inline():
    progress = []
    parallel_map(
        lambda x: x,
        [10, 20, 30],
        concurrency=1,
        on_progress=lambda done, total: progress.append((done, total)),
    )
    assert progress == [(1, 3), (2, 3), (3, 3)]


def test_on_progress_called_once_per_item_parallel():
    progress = []
    parallel_map(
        lambda x: x,
        [10, 20, 30, 40],
        concurrency=4,
        on_progress=lambda done, total: progress.append((done, total)),
    )
    # Completion order is nondeterministic, but the count must climb 1..N
    # and the total is constant.
    assert sorted(d for d, _ in progress) == [1, 2, 3, 4]
    assert {t for _, t in progress} == {4}


def test_no_on_progress_is_fine_parallel():
    out = parallel_map(lambda x: x, [1, 2, 3], concurrency=3)
    assert out == [1, 2, 3]


def test_actually_runs_concurrently():
    """With concurrency=3 and a barrier of 3, all three must be in-flight
    at once or the barrier would deadlock (the test would time out)."""
    barrier = threading.Barrier(3, timeout=5)

    def wait_at_barrier(x):
        barrier.wait()
        return x

    out = parallel_map(wait_at_barrier, [1, 2, 3], concurrency=3)
    assert sorted(out) == [1, 2, 3]


def test_worker_exception_propagates():
    def boom(x):
        if x == 2:
            raise ValueError("kaboom")
        return x

    with pytest.raises(ValueError, match="kaboom"):
        parallel_map(boom, [1, 2, 3], concurrency=4)


def test_worker_exception_propagates_inline():
    def boom(x):
        raise RuntimeError("inline-boom")

    with pytest.raises(RuntimeError, match="inline-boom"):
        parallel_map(boom, [1], concurrency=1)
