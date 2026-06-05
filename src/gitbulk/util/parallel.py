"""Bounded, order-preserving parallel map over a thread pool.

The prune-branches scan (node ``prnpf8nq``) fans out hundreds of read-only
``gh`` calls — each a subprocess + network round-trip, so the work is I/O
bound and threads (which release the GIL across the subprocess) give a
near-linear speedup. :func:`parallel_map` is the small primitive behind
that fan-out, mirroring the ordered-result-slot pattern already used by
the dispatch executor (``gitbulk.exec``):

  - Results come back in **input order** regardless of completion order, so
    callers can ``zip(items, results)`` without tracking indices.
  - ``concurrency <= 1`` runs the work **inline** (no threads), which keeps
    the common single-item / test path deterministic and thread-free.
  - A worker exception **propagates** to the caller (the first one observed),
    rather than being silently swallowed — callers that want per-item error
    isolation catch inside ``fn`` and return a sentinel.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")

#: ``on_progress(done, total)`` — called once per completed item, from the
#: calling thread, with the running completion count and the constant total.
ProgressFn = Callable[[int, int], None]


def parallel_map(
    fn: Callable[[T], R],
    items: Iterable[T],
    *,
    concurrency: int,
    on_progress: ProgressFn | None = None,
) -> list[R]:
    """Apply ``fn`` to each of ``items`` and return results in input order.

    ``concurrency`` is the maximum number of worker threads; values ``<= 1``
    run inline. ``on_progress`` (optional) is invoked once per completed
    item with ``(done_count, total)``.
    """
    work = list(items)
    total = len(work)
    if total == 0:
        return []

    results: list[R] = [None] * total  # type: ignore[list-item]

    if concurrency <= 1:
        for i, item in enumerate(work):
            results[i] = fn(item)
            if on_progress is not None:
                on_progress(i + 1, total)
        return results

    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_to_index = {
            pool.submit(fn, item): i for i, item in enumerate(work)
        }
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            results[idx] = future.result()
            done += 1
            if on_progress is not None:
                on_progress(done, total)
    return results


__all__ = ["parallel_map", "ProgressFn"]
