"""Garbage collection helpers for ~/.cache/gitbulk/.

Track A of this.i tension ``jw3kpn4q``: a minimum-viable retention sweep
that lands in Phase 1D so Phase 2's ``report`` subcommand cannot grow
the runs/ directory unboundedly. Track B (the full ``gitbulk gc``
subcommand) remains deferred to Phase 5/6.

What's here:
    prune_runs(subcommand, retain, ...)   — keep newest N runs of one
                                            subcommand, delete the rest.

What's deferred to Phase 4+:
    sweep_orphan_worktrees(...)           — paired with worktree creation
                                            in dispatch.

Cron-log pruning is intentionally NOT done here; that lives in
``bin/gitbulk-cron`` because the log paths are the wrapper's
concern, not the Python library's (see node tp4kq2nr).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from gitbulk import paths


def prune_runs(
    subcommand: str,
    retain: int,
    *,
    runs_root: Path | None = None,
) -> list[Path]:
    """Delete ``runs/<old>-<subcommand>/`` dirs beyond the newest ``retain``.

    The ``latest-<subcommand>`` symlink's current target is always preserved
    even if it would otherwise be pruned (defensive — never strand the
    symlink). Returns the list of paths that were deleted, in deletion
    order, for the caller to log.

    Args:
        subcommand: only prune dirs ending in ``-<subcommand>`` (other
            subcommands' runs are not the caller's concern).
        retain: keep this many newest matching dirs (>= 1). Caller is
            responsible for validating; this function asserts.
        runs_root: override for ``paths.runs_dir()``; mostly for testing.
    """
    if retain < 1:
        raise ValueError(f"retain must be >= 1, got {retain}")
    root = runs_root if runs_root is not None else paths.runs_dir()
    if not root.is_dir():
        return []

    suffix = f"-{subcommand}"
    # Exclude symlinks: ``latest-<subcommand>`` also ends with the suffix and
    # ``Path.is_dir()`` follows symlinks, so without this filter the symlink
    # would shadow real run dirs in the sort.
    candidates: list[Path] = [
        p
        for p in root.iterdir()
        if p.is_dir() and not p.is_symlink() and p.name.endswith(suffix)
    ]
    # Names start with the compact ISO timestamp (paths.new_runid format), so
    # lexicographic sort = chronological sort.
    candidates.sort(key=lambda p: p.name, reverse=True)

    keepers = set(candidates[:retain])

    # Defensive: preserve the symlink target even if it's not in the top N.
    symlink = (
        runs_root / f"latest-{subcommand}"
        if runs_root is not None
        else paths.latest_run_symlink(subcommand)
    )
    if symlink.is_symlink() or symlink.exists():
        try:
            target = symlink.resolve(strict=True)
            keepers.add(target)
        except (OSError, FileNotFoundError):
            pass  # dangling symlink — nothing to preserve

    deleted: list[Path] = []
    for candidate in candidates[retain:]:
        if candidate in keepers:
            continue
        shutil.rmtree(candidate)
        deleted.append(candidate)
    return deleted
