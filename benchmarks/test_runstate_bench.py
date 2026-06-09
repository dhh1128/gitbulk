"""Perf baseline for the per-run state.yaml write path (node 5agg / PERF-F3).

Run on demand (NOT part of the coverage gate; this directory is outside
``testpaths``)::

    uv run pytest benchmarks/ --benchmark-only

This is the baseline that confirms the node 7gpd fix landed: a full
``RunState`` cycle that records 200 per-repo entries and flushes once should
now cost a single state.yaml serialization (O(n)) rather than 200 full-file
rewrites of a growing dict (O(n^2)). Re-running this after any change to
``runstate.py``'s write path catches a regression back toward O(n^2).

Requires ``pytest-benchmark`` (in the ``test`` extra). The ``benchmark``
fixture and the synthetic 200-repo scale mirror the stated 150-205 repo
production fleet.
"""

from __future__ import annotations

import pytest

from gitbulk import paths
from gitbulk.runstate import RunState

pytestmark = pytest.mark.benchmark

#: Top of the stated production fleet size (150-205 repos).
_FLEET_SIZE = 200


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    paths.ensure_directories()
    return tmp_path


def _record_fleet_cycle() -> None:
    """One full run: begin → 200 per-repo records → single flush."""
    rs = RunState.begin("merge", [], {})
    for i in range(_FLEET_SIZE):
        rs.record_repo_state(
            f"owner/repo{i:03d}",
            {"prs_seen": i, "merged": i % 2, "skipped": []},
        )
    rs.flush_state()


def test_runstate_200_repo_record_and_flush(benchmark, isolated_cache):
    # pedantic keeps the round count bounded so the baseline doesn't create
    # thousands of run dirs; iterations=1 since each call mutates the cache.
    benchmark.pedantic(_record_fleet_cycle, rounds=20, iterations=1)
