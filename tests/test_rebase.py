"""Tests for gitbulk.rebase (git rebase + force-push ops).

All git invocations are mocked; no real repo is touched (AGENTS.md
'no network in tests' + the local-git safety contract). The single
seam is gitbulk.rebase._git / subprocess.run.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gitbulk.rebase import (
    RebaseError,
    RebaseStatus,
    force_push_with_lease,
    rebase_onto_base,
)


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _seq(*outcomes):
    """side_effect that returns each outcome in order; repeats last."""
    state = {"i": 0}

    def run(*a, **k):
        i = min(state["i"], len(outcomes) - 1)
        state["i"] += 1
        return outcomes[i]

    return run


WT = Path("/tmp/wt")


# ─── rebase_onto_base ──────────────────────────────────────────────────────


def test_rebase_clean():
    # fetch ok, rebase ok
    side = _seq(_Completed(0), _Completed(0))
    with patch("gitbulk.rebase.subprocess.run", side_effect=side) as run:
        result = rebase_onto_base(WT, "dev")
    assert result.status is RebaseStatus.CLEAN
    # argv: fetch origin dev, then rebase origin/dev
    fetch_argv = run.call_args_list[0][0][0]
    rebase_argv = run.call_args_list[1][0][0]
    assert fetch_argv == ["git", "-C", str(WT), "fetch", "origin", "dev"]
    assert rebase_argv == ["git", "-C", str(WT), "rebase", "origin/dev"]


def test_rebase_fetch_failure_is_error():
    side = _seq(_Completed(1, stderr="network down"))
    with patch("gitbulk.rebase.subprocess.run", side_effect=side):
        result = rebase_onto_base(WT, "dev")
    assert result.status is RebaseStatus.ERROR
    assert "network down" in result.detail


def test_rebase_conflict_preserves():
    """rebase exits non-zero AND worktree is in conflict → CONFLICT,
    no abort (worktree left mid-rebase)."""
    side = _seq(
        _Completed(0),               # fetch
        _Completed(1, stderr="CONFLICT"),  # rebase
        # _conflicted_files: git diff --name-only --diff-filter=U
        _Completed(0, stdout="src/foo.py\nsrc/bar.py\n"),
    )
    with patch("gitbulk.rebase.subprocess.run", side_effect=side) as run, \
         patch("gitbulk.rebase.is_worktree_in_conflict", return_value=True):
        result = rebase_onto_base(WT, "dev")
    assert result.status is RebaseStatus.CONFLICT
    assert "src/foo.py" in result.detail
    assert "src/bar.py" in result.detail
    # No `rebase --abort` was issued (worktree preserved).
    argvs = [c[0][0] for c in run.call_args_list]
    assert ["git", "-C", str(WT), "rebase", "--abort"] not in argvs


def test_rebase_conflict_files_unknown():
    """Conflict but the diff query fails → still CONFLICT, generic detail."""
    side = _seq(
        _Completed(0),                       # fetch
        _Completed(1),                       # rebase
        _Completed(1, stderr="diff failed"), # _conflicted_files fails
    )
    with patch("gitbulk.rebase.subprocess.run", side_effect=side), \
         patch("gitbulk.rebase.is_worktree_in_conflict", return_value=True):
        result = rebase_onto_base(WT, "dev")
    assert result.status is RebaseStatus.CONFLICT
    assert "files unknown" in result.detail


def test_rebase_nonconflict_failure_aborts():
    """rebase non-zero but NOT a conflict → abort + ERROR."""
    side = _seq(
        _Completed(0),                          # fetch
        _Completed(1, stderr="bad revision"),   # rebase
        _Completed(0),                          # rebase --abort
    )
    with patch("gitbulk.rebase.subprocess.run", side_effect=side) as run, \
         patch("gitbulk.rebase.is_worktree_in_conflict", return_value=False):
        result = rebase_onto_base(WT, "dev")
    assert result.status is RebaseStatus.ERROR
    assert "bad revision" in result.detail
    argvs = [c[0][0] for c in run.call_args_list]
    assert ["git", "-C", str(WT), "rebase", "--abort"] in argvs


def test_rebase_nonconflict_failure_empty_stderr():
    side = _seq(
        _Completed(0),       # fetch
        _Completed(1, stderr=""),  # rebase, no stderr
        _Completed(0),       # abort
    )
    with patch("gitbulk.rebase.subprocess.run", side_effect=side), \
         patch("gitbulk.rebase.is_worktree_in_conflict", return_value=False):
        result = rebase_onto_base(WT, "dev")
    assert result.status is RebaseStatus.ERROR
    assert "no conflict markers" in result.detail


# ─── force_push_with_lease ─────────────────────────────────────────────────


def test_force_push_with_lease_argv():
    side = _seq(_Completed(0))
    with patch("gitbulk.rebase.subprocess.run", side_effect=side) as run:
        force_push_with_lease(WT, "feat/x", "a" * 40)
    argv = run.call_args_list[0][0][0]
    assert argv == [
        "git", "-C", str(WT), "push",
        f"--force-with-lease=feat/x:{'a' * 40}",
        "origin", "HEAD:feat/x",
    ]


def test_force_push_lease_violation_raises():
    side = _seq(_Completed(1, stderr="stale info: remote ref moved"))
    with patch("gitbulk.rebase.subprocess.run", side_effect=side):
        with pytest.raises(RebaseError) as exc:
            force_push_with_lease(WT, "feat/x", "a" * 40)
    assert "remote ref moved" in (exc.value.stderr or "")
